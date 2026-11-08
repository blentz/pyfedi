from datetime import timedelta

from flask import current_app, g
from sqlalchemy import text, and_
from sqlakeyset import InvalidPage, get_page
from sqlalchemy.exc import IntegrityError

from app import db, plugins, cache
from app.api.alpha.views import post_view, post_report_view, reply_view, community_view, user_view, scheduled_post_view, reply_stub_view, post_removal_ack_view, galleries_for_posts
from app.activitypub.util import normalise_actor_string
from app.constants import *
from app.storefront import BOOST_WINDOW, current_storefront, boosted_posts, domain_authority, unboosted_order_by
from app.local_names import named_by_actor
from app.models import Post, Community, utcnow, User, Feed, PostReply, PostVote, CommunityFlair, Poll, Report
# The module, not the names: app.shared.post reaches this file through a blueprint
# package before they are defined (import cycle: app.shared.post)
import app.shared.post as shared_post
from app.post.util import post_replies, get_comment_branch, tags_to_string, flair_to_string
from app.utils import post_labels, record_label_change, can_moderate_at_all, authorise_api_user, \
    site_language_id, joined_or_modding_communities, \
    in_sorted_list, instance_sticky_posts, instance_sticky_post_ids, \
    communities_banned_from_all_users, moderating_communities_ids_all_users, SqlKeysetPagination, \
    paginate_post_ids, post_ids_to_models, user_access, moderating_communities_ids, reachable_community_ids
from app.shared.tasks import task_selector
from app.local_only import hidden_here, prime_reporters, without_hidden_here, without_hidden_reports, \
    without_local_only_actors
from app.api.alpha.utils.found import a_community, a_post, a_reply, a_report
from app.visibility import can_view, visible_to_clause
# The module, not the names: app.reads.posts reaches this file through app.feed.routes before they are defined
# (import cycle: reads.posts)
import app.reads.posts as reads_posts
import app.reads.post as reads_post
from app.reads.viewer import Restrictions


def a_visible_post(post_id, auth):
    """a_post for an action: a post the caller may not see is `post not found`, as if nobody held the id.
    Checked before the action runs. Moderators get no exemption (D19)."""
    post = a_post(post_id)
    user_id = authorise_api_user(auth) if auth else None
    if not can_view(post, user_id):
        raise Exception('post not found')
    return post


def a_moderatable_post(post_id, auth):
    """a_visible_post for a delete or removal. D19 restricts seeing, not enforcement: a moderator or admin of the
    post's community may act on a followers-only post they cannot view."""
    post = a_post(post_id)
    user = authorise_api_user(auth, return_type='model') if auth else None
    if user is not None and can_moderate_at_all(post.community, user):
        return post
    return a_visible_post(post_id, auth)



def get_post_list(auth, data, user_id=None, search_type='Posts') -> dict:
    type = data['type_'] if 'type_' in data else "All"
    sort = data['sort'] if 'sort' in data else "Hot"
    if 'page_cursor' in data:
        # D895 follow-up: no schema declares page_cursor, so this is the caller's raw
        # string, and one that is not a number is their mistake -- a 400, not a 500.
        try:
            page = int(data['page_cursor'])
        except ValueError:
            raise Exception('invalid page_cursor')
    elif 'page' in data:
        page = int(data['page'])
    else:
        page = 1
    limit = int(data['limit']) if 'limit' in data else 50
    liked_only = data['liked_only'] if 'liked_only' in data else False
    saved_only = data['saved_only'] if 'saved_only' in data else False
    ignore_sticky = data['ignore_sticky'] if 'ignore_sticky' in data else False
    minimum_upvotes = data['minimum_upvotes'] if 'minimum_upvotes' in data and data['minimum_upvotes'] else None
    nsfw = data['nsfw'] if 'nsfw' in data else ''

    query = data['q'] if 'q' in data else ''

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    if auth:
        user_id = authorise_api_user(auth)

    # get the user to check if the user has hide_read posts set later down the function
    if user_id:
        user = db.session.get(User, user_id)
        g.user = user   # save the currently logged in user into g, to save loading it up again and again in post_view.

    # user_id: the logged in user
    # person_id: the author of the posts being requested

    # Asked without an account, these three used to fall through to the `else`
    # arm below and answer with the All listing instead -- a different question,
    # answered without saying so. app/api/alpha/utils/reply.py and
    # app/api/alpha/utils/community.py refuse the same three by name.
    if user_id is None and type in ('Subscribed', 'Moderating', 'ModeratorView'):
        raise Exception('incorrect login')

    community_id = int(data['community_id']) if 'community_id' in data else None
    feed_id = int(data['feed_id']) if 'feed_id' in data else None
    topic_id = int(data['topic_id']) if 'topic_id' in data else None
    community_name = data['community_name'] if 'community_name' in data else None
    person_id = int(data['person_id']) if 'person_id' in data else None

    if community_id or community_name:
        reads_post.refuse_private_community(community_id, community_name, user_id, user if user_id else None)

    # a reader of their own posts is exempt from their own blocks
    restrictions = Restrictions(user_id) if user_id and user_id != person_id else Restrictions.none()
    listing = reads_posts.Listing(type=type, sort=sort, community_id=community_id, community_name=community_name,
                      feed_id=feed_id, topic_id=topic_id, person_id=person_id, liked_only=liked_only,
                      saved_only=saved_only, ignore_sticky=ignore_sticky, minimum_upvotes=minimum_upvotes,
                      nsfw=nsfw, query=query, search_type=search_type)
    named_community = None
    if community_name:
        if '@' not in community_name:
            community_name = f"{community_name}@{domain_authority(current_storefront())}"   # the storefront's
        name, ap_domain = normalise_actor_string(community_name)
        # No guard here: refuse_private_community refuses a malformed `community_name` before this is reached
        # (when no community_id is named instead), and ('', '') simply matches nothing.
        named_community = and_(Community.name == name, Community.ap_domain == ap_domain)
    built = reads_posts.post_listing(user if user_id else None, restrictions, listing, reads_posts.OFFSET_PAGES, named_community)
    content_filters = built.content_filters
    u_rp_ids = built.read_post_ids

    use_faster_query = built.fast is not None
    if use_faster_query:
        post_ids = reads_posts.fast_post_ids(built)
        # `page * limit`, not `page + 1 * limit`. `*` binds tighter than `+`, so
        # the test read `len > page + limit` and stayed true long after the rows
        # ran out: page 6 of a nine-row listing at two per page came back empty
        # AND said next_page was 7, which an infinite-scroll client follows
        # forever.
        has_next_page = len(post_ids) > page * limit
        post_ids = paginate_post_ids(post_ids, page - 1, page_length=limit)
        posts = post_ids_to_models(post_ids, sort.lower()).all()
    else:
        posts = built.posts
        if built.boosted:     # past the boosted window, the plain order the index reads straight to the OFFSET
            posts = boosted_posts(posts, *built.boosted, page * limit) if page * limit <= BOOST_WINDOW else \
                posts.order_by(*unboosted_order_by(*built.boosted))
        posts = posts.paginate(page=page, per_page=limit, error_out=False)

    if user_id:

        banned_from = communities_banned_from_all_users()

        bookmarked_posts = list(db.session.execute(text(
            'SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
            {'user_id': user_id}).scalars())
        post_subscriptions = list(db.session.execute(text(
            'SELECT entity_id FROM "notification_subscription" WHERE type = :type and user_id = :user_id'),
            {'type': NOTIF_POST, 'user_id': user_id}).scalars())
        read_post_set = set(u_rp_ids)  # lookups ("in") on a set is O(1), tuples/lists are O(n). read_posts can be very large so this makes a difference.

        communities_moderating = moderating_communities_ids_all_users()
        communities_joined = joined_or_modding_communities(user.id)

        # collect all unique author IDs from posts, then only get user_notes relating to them
        author_ids = set()
        post_ids = []
        for post in posts if use_faster_query else posts.items:
            author_ids.add(post.user_id)
            post_ids.append(post.id)

        if author_ids:
            usernotes_query = db.session.execute(
                text('SELECT target_id, body FROM "user_note" WHERE user_id = :user_id AND target_id IN :author_ids'),
                {'user_id': user_id, 'author_ids': tuple(author_ids)}
            ).all()
            usernotes = {note[0]: note[1] for note in usernotes_query}
        else:
            usernotes = None
        
        user_votes = get_post_votes_for_posts(user_id, post_ids)
        unread_counts = get_post_unread_counts(user_id, post_ids)
        interacted_at = get_post_interacted_at(user_id, post_ids)
    else:
        bookmarked_posts = []
        banned_from = {}
        post_subscriptions = []
        read_post_set = set()
        communities_moderating = []
        communities_joined = []
        usernotes = {}
        user_votes = {}
        unread_counts = {}
        interacted_at = {}

    galleries = galleries_for_posts(posts)
    postlist = []
    for post in posts:
        # Get the pre-fetched vote, default to 0 if not found
        my_vote = user_votes.get(post.id, 0)
        
        postlist.append(post_view(post=post, variant=2, stub=False, user_id=user_id,
                                  communities_moderating=communities_moderating,
                                  banned_from=banned_from, bookmarked_posts=bookmarked_posts,
                                  post_subscriptions=post_subscriptions, read_posts=read_post_set,
                                  communities_joined=communities_joined, content_filters=content_filters,
                                  usernotes=usernotes, my_vote=my_vote, unread_counts=unread_counts,
                                  interacted_at=interacted_at, galleries=galleries))
    if use_faster_query:
        list_json = {
            "posts": postlist,
            "next_page": str(page + 1) if has_next_page else None
        }
    else:
        list_json = {
            "posts": postlist,
            "next_page": str(posts.next_num) if posts.next_num is not None else None
        }

    return list_json


def get_scheduled_post_list(auth, data):
    sort = data['sort'] if 'sort' in data else "New"
    if 'page_cursor' in data:
        page = int(data['page_cursor'])
    elif 'page' in data:
        page = int(data['page'])
    else:
        page = 1
    limit = int(data['limit']) if 'limit' in data else 50
    community_id = int(data['community_id']) if 'community_id' in data else None
    community_name = data['community_name'] if 'community_name' in data else None
    query = data['q'] if 'q' in data else ''
    include_deleted = data['include_deleted'] if 'include_deleted' in data else False

    user_id = authorise_api_user(auth) if auth else None
    if not user_id:
        raise Exception("incorrect login")

    # Handle special sort case, ignore relevance sorting if not searching
    if not query and sort == "Relevance":
        sort = "New"

    # Start by getting all scheduled posts for the user
    scheduled_posts = Post.query.filter(Post.status == POST_STATUS_SCHEDULED, Post.user_id == user_id)

    if not include_deleted:
        scheduled_posts = scheduled_posts.filter(Post.deleted == False)

    # Filter by community
    if community_id or community_name:
        community = None
        if community_id:
            community = db.session.get(Community, community_id)
        else:
            # A bare name is local on the request's own domain; a handle on a domain of ours is that domain's
            community = Community.query.filter(named_by_actor(Community, community_name.strip())).first()
        if community is not None and hidden_here(community, True):
            community = None   # a community hidden here answers as one that does not exist (follow-up F6)

        if community:
            scheduled_posts = scheduled_posts.filter(Post.community_id == community.id)
        else:
            if community_id:
                raise Exception("Community not found")
            else:
                raise Exception("Community not found. Check to make sure the name is accurate as community_name is case-sensitive")

    # Filter with a search
    if query:
        scheduled_posts = scheduled_posts.search(query, sort=sort == 'Relevance')

    # Sort the results
    if sort == "Old":
        scheduled_posts = scheduled_posts.order_by(Post.scheduled_for.desc())
    elif sort == "New":
        scheduled_posts = scheduled_posts.order_by(Post.scheduled_for)

    # Paginate the results
    scheduled_posts = scheduled_posts.paginate(page=page, per_page=limit, error_out=False)

    post_list = []

    # Build the json for the posts
    for scheduled_post in scheduled_posts.items:
        post_list.append(scheduled_post_view(post=scheduled_post, variant=2, user_id=user_id)["scheduled_post_view"])

    list_json = {"scheduled_posts": post_list,
                 "next_page": str(scheduled_posts.next_num) if scheduled_posts.next_num is not None else None}

    return list_json


def get_post_votes_for_posts(user_id, post_ids):
    """Pre-fetch user votes for a list of posts to avoid N+1 queries in post_view()"""
    if not user_id or not post_ids:
        return {}
    
    votes = db.session.execute(
        text('SELECT post_id, effect FROM "post_vote" WHERE user_id = :user_id AND post_id IN :post_ids'),
        {'user_id': user_id, 'post_ids': tuple(post_ids)}
    ).all()
    
    # Convert to a dictionary for O(1) lookup
    return {vote[0]: vote[1] for vote in votes}


def get_post_unread_counts(user_id, post_ids):
    """Pre-fetch user unread comment counts for a list of posts to avoid N+1 queries in post_view()"""
    if not user_id or not post_ids:
        return {}

    counts = db.session.execute(
        text("""SELECT
                    p.id AS post_id,
                    CASE
                        WHEN rp.read_post_id IS NOT NULL
                        THEN COUNT(CASE WHEN pr.posted_at >= rp.interacted_at THEN 1 END)
                        ELSE p.reply_count
                    END AS reply_count
                FROM
                    post p
                LEFT JOIN
                    read_posts rp ON rp.read_post_id = p.id AND rp.user_id = :user_id
                LEFT JOIN
                    post_reply pr ON pr.post_id = p.id
                WHERE
                    p.id IN :post_ids
                GROUP BY
                    p.id, p.reply_count, rp.read_post_id, rp.interacted_at;"""),
        {'user_id': user_id, 'post_ids': tuple(post_ids)}
    ).all()

    # Convert to a dictionary for O(1) lookup
    return {c[0]: c[1] for c in counts}


def get_post_interacted_at(user_id, post_ids):
    """Pre-fetch user interacted_at for a list of posts to avoid N+1 queries in post_view()"""
    if not user_id or not post_ids:
        return {}

    interacted = db.session.execute(
        text('SELECT read_post_id, interacted_at FROM "read_posts" WHERE read_post_id IN :post_ids and user_id = :user_id'),
        {'user_id': user_id, 'post_ids': tuple(post_ids)}
    ).all()

    # Convert to a dictionary for O(1) lookup
    return {i[0]: i[1] for i in interacted}


def ordered_by_boost(posts, rank, tail, limit, bookmark, lead):
    """`boosted_posts`, a cursor it refuses refused as the keyset page refuses one."""
    try:
        return boosted_posts(posts, rank, tail, limit, bookmark, lead)
    except InvalidPage:
        raise Exception('invalid page_cursor')


def get_post_list2(auth, data, user_id=None, search_type='Posts') -> dict:
    type = data['type_'] if 'type_' in data else "All"
    sort = data['sort'] if 'sort' in data else "Hot"
    if 'page_cursor' in data:
        bookmark = data['page_cursor']
    elif 'page' in data:
        bookmark = data['page']
    else:
        bookmark = None
    limit = int(data['limit']) if 'limit' in data else 50
    liked_only = data['liked_only'] if 'liked_only' in data else False
    saved_only = data['saved_only'] if 'saved_only' in data else False
    ignore_sticky = data['ignore_sticky'] if 'ignore_sticky' in data else False
    minimum_upvotes = data['minimum_upvotes'] if 'minimum_upvotes' in data and data['minimum_upvotes'] else None
    nsfw = data['nsfw'] if 'nsfw' in data else ''

    query = data['q'] if 'q' in data else ''

    # The same clamp get_post_list applies. Without it a caller naming
    # `limit=100000` had every one of those rows built into Post objects and
    # rendered into the response.
    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    if auth:
        user_id = authorise_api_user(auth)

    # get the user to check if the user has hide_read posts set later down the function

    if user_id:
        user = db.session.get(User, user_id)
        g.user = user   # save the currently logged in user into g, to save loading it up again and again in post_view.

    # user_id: the logged in user
    # person_id: the author of the posts being requested

    # Asked without an account, these three used to fall through to the `else`
    # arm below and answer with the All listing instead -- a different question,
    # answered without saying so. app/api/alpha/utils/reply.py and
    # app/api/alpha/utils/community.py refuse the same three by name.
    if user_id is None and type in ('Subscribed', 'Moderating', 'ModeratorView'):
        raise Exception('incorrect login')

    community_id = int(data['community_id']) if 'community_id' in data else None
    feed_id = int(data['feed_id']) if 'feed_id' in data else None
    topic_id = int(data['topic_id']) if 'topic_id' in data else None
    community_name = data['community_name'] if 'community_name' in data else None
    person_id = int(data['person_id']) if 'person_id' in data else None

    if community_id or community_name:
        reads_post.refuse_private_community(community_id, community_name, user_id, user if user_id else None)

    # a reader of their own posts is exempt from their own blocks
    restrictions = Restrictions(user_id) if user_id and user_id != person_id else Restrictions.none()
    listing = reads_posts.Listing(type=type, sort=sort, community_id=community_id, community_name=community_name,
                      feed_id=feed_id, topic_id=topic_id, person_id=person_id, liked_only=liked_only,
                      saved_only=saved_only, ignore_sticky=ignore_sticky, minimum_upvotes=minimum_upvotes,
                      nsfw=nsfw, query=query, search_type=search_type)
    named_community = None
    if community_name:
        if '@' not in community_name:
            community_name = f"{community_name}@{domain_authority(current_storefront())}"   # the storefront's
        name, ap_domain = normalise_actor_string(community_name)
        # No guard here: refuse_private_community refuses a malformed `community_name` before this is reached
        # (when no community_id is named instead), and ('', '') simply matches nothing.
        named_community = and_(Community.name == name, Community.ap_domain == ap_domain)
    built = reads_posts.post_listing(user if user_id else None, restrictions, listing, reads_posts.KEYSET_PAGES, named_community)
    content_filters = built.content_filters
    u_rp_ids = built.read_post_ids
    posts = built.posts
    if built.boosted:
        # A cursor of these two sorts carries the boosted value, which is the storefront's: replayed on another domain
        # it can skip or repeat rows (the filters above still apply, so nothing is shown that was not already)
        posts = ordered_by_boost(posts, *built.boosted, limit, bookmark, built.sticky_columns)

    try:
        page_obj = get_page(posts, per_page=limit, page=bookmark)
    except InvalidPage:  # also BadBookmark: a cursor this server never handed out (D895 follow-up)
        raise Exception('invalid page_cursor')
    posts = SqlKeysetPagination(page_obj)

    if user_id:

        banned_from = communities_banned_from_all_users()

        bookmarked_posts = list(db.session.execute(text(
            'SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
            {'user_id': user_id}).scalars())
        post_subscriptions = list(db.session.execute(text(
            'SELECT entity_id FROM "notification_subscription" WHERE type = :type and user_id = :user_id'),
            {'type': NOTIF_POST, 'user_id': user_id}).scalars())
        read_post_set = set(u_rp_ids)  # lookups ("in") on a set is O(1), tuples/lists are O(n). read_posts can be very large so this makes a difference.

        communities_moderating = moderating_communities_ids_all_users()
        communities_joined = joined_or_modding_communities(user.id)
        
        # Pre-fetch user votes to avoid N+1 queries in post_view()
        # posts.items contains Post objects directly
        post_objects = list(posts.items)
        post_ids = [post.id for post in post_objects]
        user_votes = get_post_votes_for_posts(user_id, post_ids)
    else:
        bookmarked_posts = []
        banned_from = {}
        post_subscriptions = []
        read_post_set = set()
        communities_moderating = []
        communities_joined = []
        user_votes = {}

    galleries = galleries_for_posts(posts.items)
    postlist = []
    for post in posts.items:
        # Get the pre-fetched vote, default to 0 if not found
        my_vote = user_votes.get(post.id, 0)
        
        postlist.append(post_view(post=post, variant=2, stub=False, user_id=user_id,
                                  communities_moderating=communities_moderating,
                                  banned_from=banned_from, bookmarked_posts=bookmarked_posts,
                                  post_subscriptions=post_subscriptions, read_posts=read_post_set,
                                  communities_joined=communities_joined, content_filters=content_filters,
                                  my_vote=my_vote, galleries=galleries))

    list_json = {
        "posts": postlist,
        "next_page": posts.next_bookmark if posts.has_next else None
    }

    return list_json


def get_post(auth, data):
    if not data or 'id' not in data:
        raise Exception('missing parameters for post')

    try:
        id = int(data['id'])
    except (TypeError, ValueError):
        # `int('abc')` reached the caller as
        # "invalid literal for int() with base 10: 'abc'".
        raise Exception('id must be a number')

    # a_post before post_view: the view looks the row up with `.one()`, so an
    # id nobody holds answered the bare `NoResultFound: ()`.
    post = a_post(id)

    user_id = authorise_api_user(auth) if auth else None
    reads_post.refuse_unreadable_post(post, user_id, Restrictions(user_id))

    post_json = post_view(post=post, variant=3, user_id=user_id)
    return post_json


def get_scheduled_post(auth, data):
    id = data['id']

    user_id = authorise_api_user(auth) if auth else None
    if not user_id:
        raise Exception("incorrect login")

    post_json = scheduled_post_view(post=id, variant=2, user_id=user_id)
    return post_json


def get_post_replies(auth, data):
    sort = data['sort'] if 'sort' in data else 'New'
    max_depth = int(data['max_depth']) if 'max_depth' in data else None
    page_cursor = data['page'] if 'page' in data else None  # Expects reply ID or None for first page
    limit = int(data['limit']) if 'limit' in data else 20
    post_id = data['post_id'] if 'post_id' in data else None
    parent_id = data['parent_id'] if 'parent_id' in data else None

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    if auth:
        user_details = authorise_api_user(auth, return_type='dict')
        user_id = user_details['id']
        # get_comment_branch() is borrowed from the web-ui so needs the full User
        user = User.query.filter_by(id=user_id).one()
    else:
        user_details = {}
        user_id = None
        user = None

    if parent_id:
        parent = a_reply(parent_id)   # hidden here or nobody's: one answer (follow-up F6)
        if post_id is None:
            post_id = parent.post_id
        post = a_visible_post(post_id, auth)
        replies = get_comment_branch(post, parent.id, sort.lower(), user)
    else:
        # Naming neither leaves post_id None, and `db.session.get(Post, None)`
        # answers None after warning that a fully NULL primary key identity
        # cannot load any object -- so a request with no post_id at all was
        # `'NoneType' object has no attribute 'archived'` from inside
        # post_replies, and so was one naming a post nobody holds.
        if post_id is None:
            raise Exception('post_id or parent_id required')
        post = a_visible_post(post_id, auth)
        replies = post_replies(post, sort.lower(), user)

    is_user_banned_from_community = post.community_id in user_details['user_ban_community_ids'] if user_details else False
    is_user_following_community = post.community_id in user_details['followed_community_ids'] if user_details else False
    is_user_moderator = post.community_id in user_details['moderated_community_ids'] if user_details else False

    # Apply max_depth filter to the nested reply tree
    def filter_max_depth(reply_tree, current_depth=0, parent_depth=0):
        """Filter nested reply tree by max_depth.

        The caller below guards `max_depth is not None`, so the guard that
        used to stand here for the same thing cannot run. It was the right
        test in the wrong place: the caller's own guard read `if max_depth:`,
        which never called this function at all for a max_depth of 0.
        """
        filtered_tree = []
        for item in reply_tree:
            comment = item['comment']
            # Calculate depth relative to parent_id if specified, otherwise use absolute depth
            effective_depth = current_depth if parent_id else comment.depth

            if effective_depth <= max_depth:
                filtered_item = {
                    **item,
                    'replies': filter_max_depth(item['replies'], current_depth + 1, parent_depth)
                }
                filtered_tree.append(filtered_item)

        return filtered_tree

    # Apply max_depth filter
    # `is not None`, not truthiness: max_depth=0 means "the top level and
    # nothing under it", and 0 is falsy, so the one depth a caller is most
    # likely to ask for was the one silently ignored. filter_max_depth's own
    # guard already tests `is None`.
    if max_depth is not None:
        replies = filter_max_depth(replies)

    # Apply cursor-based pagination
    def paginate_with_cursor(reply_tree, cursor_id, limit):
        """Paginate using reply ID cursor while keeping complete branches together"""
        if not reply_tree:
            return [], None

        # Find starting position based on cursor
        start_index = 0
        if cursor_id:
            try:
                cursor_id = int(cursor_id)
                # Find the top-level branch that matches this cursor
                for i, branch in enumerate(reply_tree):
                    if branch['comment'].id == cursor_id:
                        start_index = i  # Start from the cursor branch itself
                        break
                else:
                    # Cursor not found, return empty (past end)
                    return [], None
            except (ValueError, TypeError):
                # Invalid cursor, start from beginning
                start_index = 0

        # Count flattened items in branches to respect limit
        def get_branch_size(branch):
            """Count total items in a branch (including all descendants)"""
            count = 1  # The branch itself
            for child in branch['replies']:
                count += get_branch_size(child)
            return count

        # Collect branches starting from cursor position
        included_branches = []
        total_items = 0

        for i in range(start_index, len(reply_tree)):
            branch = reply_tree[i]
            branch_size = get_branch_size(branch)

            # Always include at least one branch, even if it exceeds limit
            if not included_branches:
                included_branches.append(branch)
                total_items += branch_size
            elif total_items + branch_size <= limit or total_items == 0:
                # Include this branch if it fits within limit
                included_branches.append(branch)
                total_items += branch_size
            else:
                # Would exceed limit, stop here
                break

        # Determine next cursor (ID of next top-level branch after those included)
        next_cursor = None
        if included_branches:
            last_included_index = start_index + len(included_branches) - 1
            if last_included_index + 1 < len(reply_tree):
                next_cursor = reply_tree[last_included_index + 1]['comment'].id

        return included_branches, next_cursor

    # Apply pagination
    replies, next_cursor = paginate_with_cursor(replies, page_cursor, limit)

    inner_post_view = None
    inner_community_view = None

    # Precompute once for all replies in this post
    community_moderator_ids = {m.user_id for m in post.community.moderators()}
    all_ban_data = communities_banned_from_all_users()

    stub_neutrals = {}  # D7: one neutral community and post for every stub in this thread

    # Process nested reply tree while preserving structure
    def process_nested_replies(reply_tree, is_top_level=True):
        """Process nested reply tree while preserving nested structure"""
        nonlocal inner_post_view, inner_community_view
        nonlocal is_user_banned_from_community, is_user_following_community, is_user_moderator
        processed_replies = []

        for item in reply_tree:
            reply = item['comment']
            if item.get('restricted'):
                # D18: the place in the tree, nothing of the reply; its visible children stay
                stub = reply_stub_view(reply, stub_neutrals)
                stub['replies'] = process_nested_replies(item['replies'], is_top_level=False)
                processed_replies.append(stub)
                continue
            is_reply_bookmarked = reply.id in user_details['bookmarked_reply_ids'] if user_details else None
            is_creator_blocked = reply.user_id in user_details['blocked_creator_ids'] if user_details else False
            vote_effect = 0
            if user_details and in_sorted_list(user_details['upvoted_reply_ids'], reply.id):
                vote_effect = 1
            elif user_details and in_sorted_list(user_details['downvoted_reply_ids'], reply.id):
                vote_effect = -1
            is_reply_subscribed = reply.id in user_details['subscribed_reply_ids'] if user_details else None
            is_creator_banned = post.community_id in all_ban_data.get(reply.user_id, [])
            is_creator_mod = reply.user_id in community_moderator_ids

            view = reply_view(reply=reply, variant=3, user_id=user_id,
                              is_user_banned_from_community=is_user_banned_from_community,
                              is_user_following_community=is_user_following_community,
                              is_reply_bookmarked=is_reply_bookmarked,
                              is_creator_blocked=is_creator_blocked,
                              vote_effect=vote_effect,
                              is_reply_subscribed=is_reply_subscribed,
                              is_user_moderator=is_user_moderator,
                              is_creator_banned_from_community=is_creator_banned,
                              is_creator_moderator=is_creator_mod,
                              add_post_in_view=False,
                              add_community_in_view=False)

            # Only include post and community info on top-level replies
            if is_top_level:
                if not inner_post_view:
                    inner_post_view = post_view(post, variant=1, user_id=user_id)
                view['post'] = inner_post_view
                if not inner_community_view:
                    inner_community_view = community_view(post.community, variant=1, stub=True)
                view['community'] = inner_community_view

            # Process nested replies
            if item['replies']:
                view['replies'] = process_nested_replies(item['replies'], is_top_level=False)
            else:
                view['replies'] = []

            processed_replies.append(view)

        return processed_replies

    replylist = process_nested_replies(replies)

    list_json = {
        "comments": replylist,
        "next_page": str(next_cursor) if next_cursor is not None else None
    }

    return list_json


def post_post_like(auth, data):
    # No `if not user` guard: authorise_api_user either returns a User or
    # raises -- there is no falsy return for return_type='model' -- so the
    # guard that used to stand here could not run.
    user = authorise_api_user(auth, return_type="model")

    post_id = a_visible_post(data['post_id'], auth).id
    score = data['score']
    private = data['private'] if 'private' in data else bool(user.vote_privately)
    emoji = data['emoji'] if 'emoji' in data else None
    if score == 1:
        direction = 'upvote'
    elif score == -1:
        direction = 'downvote'
    else:
        score = 0
        direction = 'reversal'

    user_id = shared_post.vote_for_post(post_id, direction, not private, emoji, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id, my_vote=score)
    return post_json


def put_post_save(auth, data):
    post_id = a_visible_post(data['post_id'], auth).id
    save = data['save']

    user_id = shared_post.bookmark_post(post_id, SRC_API, auth) if save else shared_post.remove_bookmark_post(post_id, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id)
    return post_json


def put_post_subscribe(auth, data):
    post_id = a_visible_post(data['post_id'], auth).id
    subscribe = data['subscribe']

    user_id = shared_post.subscribe_post(post_id, subscribe, SRC_API, auth)
    post_json = post_view(post=post_id, variant=4, user_id=user_id)
    return post_json


def _album(data) -> list | None:
    """The API's `gallery` as edit_post's: None when the key is absent (keep the pictures), else every picture of a
    gallery post (spec 2026-11-01 G-5)."""
    if 'gallery' not in data:
        return None
    return [{'url': image['url'], 'alt_text': image.get('alt_text')} for image in data['gallery']]


def post_post(auth, data):
    title = data['title']
    community_id = data['community_id']
    body = data['body'] if 'body' in data else ''
    url = data['url'] if 'url' in data else None
    nsfw = data['nsfw'] if 'nsfw' in data else False
    ai_generated = data['ai_generated'] if 'ai_generated' in data else False
    alt_text = data['alt_text'] if 'alt_text' in data else title
    language_id = data['language_id'] if 'language_id' in data else site_language_id()
    og_image_url = data.get('og_image_url', '')
    # `is None or`: site_language_id() answers None on an instance whose
    # languages are not seeded, and `None < 2` is a TypeError. Fact 478 --
    # app/api/alpha/utils/reply.py asks the same question the same way.
    if language_id is None or language_id < 2:
        language_id = site_language_id()

    # Scheduled post fields
    scheduled_for = data['scheduled_for'] if 'scheduled_for' in data else None
    repeat = data['repeat'] if 'repeat' in data else None
    
    user_id = authorise_api_user(auth)

    # Determine post type based on data provided
    if 'event' in data and data['event']:
        type = POST_TYPE_EVENT
    elif 'poll' in data and data['poll']:
        type = POST_TYPE_POLL
        if 'end_poll' not in data['poll']:
            # Default to ending the poll in three days
            end_poll = utcnow() + timedelta(days=3)
            data['poll']['end_poll'] = end_poll.isoformat(timespec="microseconds")
    elif data.get('gallery'):   # every picture of a gallery post; its url is its own page (spec 2026-11-01 G-5)
        type = POST_TYPE_GALLERY
    elif url:
        type = POST_TYPE_LINK
    else:
        type = POST_TYPE_ARTICLE

    input = {'title': title,
             'body': body,
             'url': url,
             'nsfw': nsfw,
             'language_id': language_id,
             'notify_author': True,
             'ai_generated': ai_generated,
             'image_alt_text': alt_text,
             'scheduled_for': scheduled_for,
             'repeat': repeat,
             'og_image_url': og_image_url}

    # Add event data if present
    if 'event' in data and data['event']:
        input['event'] = data['event']

    # Add poll data if present
    if 'poll' in data and data['poll']:
        input['poll'] = data['poll']

    if 'visibility' in data:
        input['visibility'] = data['visibility']

    community = a_community(community_id)

    # Fire hook for plugins
    post_data = {
        "title": title,
        "content": body,
        "og_image_url": og_image_url,
        "community": community.name,
        "community_id": community.id,
        "post_type": type,
        "user_id": user_id,
    }
    post_data = plugins.fire_hook('before_post_create', post_data)
    # plugins may rewrite the title and content, but not who is posting, where, or what type of post
    input['title'] = post_data.get('title', input['title'])
    input['body'] = post_data.get('content', input['body'])

    user_id, post = shared_post.make_post(input, community, type, SRC_API, auth, album=_album(data))

    if not scheduled_for:
        post_json = post_view(post=post, variant=4, user_id=user_id)
    else:
        post_json = scheduled_post_view(post=post, variant=2, user_id=user_id)
    
    return post_json


def put_post(auth, data):
    post_id = data['post_id']
    post = a_visible_post(post_id, auth)
    if 'visibility' in data and data['visibility'] != post.visibility:
        raise Exception('visibility cannot be changed')     # Amendment B: fixed at creation

    title = data['title'] if 'title' in data else post.title
    # `or ''`: Post.body is nullable and a link or image post routinely has no
    # body at all, while edit_post hands whatever it gets to
    # piefed_markdown_to_lemmy_markdown, which is a regex substitution --
    # `TypeError: expected string or bytes-like object, got 'NoneType'`. So no
    # bodyless post could be edited through the API, not even to fix its title.
    # post_post one function above already defaults its body to ''.
    body = data['body'] if 'body' in data else (post.body or '')
    url = data['url'] if 'url' in data else post.url
    nsfw = data['nsfw'] if 'nsfw' in data else post.nsfw
    ai_generated = data['ai_generated'] if 'ai_generated' in data else post.ai_generated
    language_id = data['language_id'] if 'language_id' in data else post.language_id
    tags = data['tags'] if 'tags' in data else tags_to_string(post) or ''
    flair = data['flair'] if 'flair' in data else flair_to_string(post) or ''
    # `is None or`: the post's own language_id is nullable. See post_post above.
    if language_id is None or language_id < 2:
        language_id = site_language_id()
    
    if 'alt_text' in data:
        alt_text = data['alt_text']
    elif post.image and post.image.alt_text:
        alt_text = post.image.alt_text
    else:
        alt_text = ''

    # Scheduled post fields
    if post.status == POST_STATUS_SCHEDULED:
        scheduled_for = data['scheduled_for'] if 'scheduled_for' in data else post.scheduled_for
        repeat = data['repeat'] if 'repeat' in data else post.repeat
    else:
        scheduled_for = None
        repeat = None

    # Determine post type - keep existing type unless explicitly changed
    type = post.type
    # Only infer type change if URL is being changed
    if 'url' in data:
        if url:
            type = POST_TYPE_LINK
        else:
            type = POST_TYPE_ARTICLE
    if data.get('gallery') or (post.type == POST_TYPE_GALLERY and 'gallery' not in data and 'url' not in data):
        type = POST_TYPE_GALLERY   # given pictures make a gallery; leaving both out keeps one (spec 2026-11-01 G-5)

    input = {'title': title,
             'body': body,
             'url': url,
             'nsfw': nsfw,
             'language_id': language_id,
             'notify_author': True,
             'tags': tags,
             'flair': flair,
             'ai_generated': ai_generated,
             'image_alt_text': alt_text,
             'scheduled_for': scheduled_for,
             'repeat': repeat}

    # Add event data if present
    if 'event' in data and data['event']:
        input['event'] = data['event']

    # Add poll data if present
    if 'poll' in data and data['poll']:
        input['poll'] = data['poll']

    user_id, post = shared_post.edit_post(input, post, type, SRC_API, auth=auth, album=_album(data))

    if not scheduled_for:
        post_json = post_view(post=post, variant=4, user_id=user_id)
    else:
        post_json = scheduled_post_view(post=post, variant=2, user_id=user_id)
    
    return post_json


def post_post_delete(auth, data):
    post_id = a_visible_post(data['post_id'], auth).id
    deleted = data['deleted']

    if deleted:
        user_id, post = shared_post.delete_post(post_id, True, SRC_API, auth)
    else:
        user_id, post = shared_post.restore_post(post_id, SRC_API, auth)

    if post.status == POST_STATUS_SCHEDULED:
        post_json = scheduled_post_view(post=post, variant=2, user_id=user_id)
    else:
        post_json = post_view(post=post, variant=4, user_id=user_id)
    
    return post_json


def post_post_report(auth, data):
    post_id = data['post_id']
    reason = data['reason']
    description = data['description'] if 'description' in data else ''
    report_remote = data['report_remote'] if 'report_remote' in data else True
    input = {'reason': reason, 'description': description, 'report_remote': report_remote}

    post = a_visible_post(post_id, auth)
    user_id, report = shared_post.report_post(post, input, SRC_API, auth)

    post_json = post_report_view(report=report, post_id=post_id, user_id=user_id)
    return post_json


def get_post_report_list(auth, data):
    # No `if not user` guard: authorise_api_user either returns a User or
    # raises -- there is no falsy return for return_type='model' -- so the
    # guard that used to stand here could not run.
    user = authorise_api_user(auth, return_type="model")

    post_id = data['post_id'] if 'post_id' in data else None
    community_id = data['community_id'] if 'community_id' in data else None
    limit = data['limit'] if 'limit' in data else 20
    page = data['page'] if 'page' in data else 1
    unresolved_only = data['unresolved_only'] if 'unresolved_only' in data else True

    if post_id:
        # Just get reports for a single post
        post = a_post(post_id)
        mods = post.community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access('moderate all communities', user.id, on=post):
            reports = Report.query.filter(Report.suspect_post_id == post_id,
                                          Report.suspect_post_reply_id == None)
        else:
            raise Exception('incorrect login')
    elif community_id:
        # Just get reports for a single community
        community = a_community(community_id)
        mods = community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access("moderate all communities", user.id, on=community):
            reports = Report.query.filter(Report.in_community_id == community_id,
                                          Report.suspect_post_id != None,
                                          Report.suspect_post_reply_id == None)
        else:
            raise Exception('incorrect login')
    else:
        # Don't restrict reports to single post or community
        reports = Report.query.filter(Report.suspect_post_id != None, Report.suspect_post_reply_id == None)
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
        report_list.append(post_report_view(report=report,
                                             post_id=report.suspect_post_id,
                                             user_id=user.id,
                                             variant=2))
    
    reply_json = dict()
    reply_json['post_reports'] = report_list
    reply_json['next_page'] = str(reports.next_num) if reports.next_num else None

    return reply_json


def put_post_report_resolve(auth, data):
    report_id = data['report_id']
    resolved = data['resolved']

    # No `if not user` guard: authorise_api_user either returns a User or
    # raises -- there is no falsy return for return_type='model' -- so the
    # guard that used to stand here could not run.
    user = authorise_api_user(auth, return_type="model")

    report = a_report(report_id)

    # `if not report.suspect_post_id`, on its own. Tested with
    # `and report.suspect_post_reply_id`, this refused a report against a
    # COMMENT and let every other kind through -- a report against a person, a
    # community or a conversation names no post and no reply, so it reached
    # `db.session.get(Community, report.in_community_id)` with None and died
    # on `community.moderators()`. This endpoint resolves post reports.
    if not report.suspect_post_id:
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
    
    reply_json = post_report_view(report=report, post_id=report.suspect_post_id, user_id=user.id)
    return reply_json


def post_post_lock(auth, data):
    post_id = a_moderatable_post(data['post_id'], auth).id
    locked = data['locked']

    user_id, post = shared_post.lock_post(post_id, locked, SRC_API, auth)

    if not can_view(post, user_id):  # as post_post_remove: an acknowledgement, not the content (D19)
        return {'post_view': post_removal_ack_view(post)}
    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_hide(auth, data):
    post_id = a_visible_post(data['post_id'], auth).id
    hidden = data['hidden']

    user_id, post = shared_post.hide_post(post_id, hidden, SRC_API, auth)

    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_feature(auth, data):
    post_id = a_moderatable_post(data['post_id'], auth).id
    featured = data['featured']
    feature_type = data['feature_type'] if 'feature_type' in data else 'Community'

    # Neither arm bound `post` or `user_id` for a feature_type that is neither,
    # so the response built below was an UnboundLocalError.
    if feature_type not in ("Community", "Local"):
        raise Exception('feature_type must be Community or Local')

    if feature_type == "Community":
        user_id, post = shared_post.sticky_post(post_id, featured, SRC_API, auth)
    else:
        user = authorise_api_user(auth, 'model')
        user_id = user.id
        post = db.session.get(Post, post_id)
        
        if user_access('feature instance-wide', user.id):
            post.instance_sticky = featured

            cache.delete_memoized(instance_sticky_posts)
            cache.delete_memoized(instance_sticky_post_ids)
            db.session.commit()
        else:
            raise Exception('Only admin users can feature a post locally')

    if not can_view(post, user_id):  # as post_post_remove: an acknowledgement, not the content (D19)
        return {'post_view': post_removal_ack_view(post)}
    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_remove(auth, data):
    post_id = a_moderatable_post(data['post_id'], auth).id
    removed = data['removed']

    if removed:
        reason = data['reason'] if 'reason' in data else 'Removed by mod'
        user_id, post = shared_post.mod_remove_post(post_id, reason, SRC_API, auth)
    else:
        reason = data['reason'] if 'reason' in data else 'Restored by mod'
        user_id, post = shared_post.mod_restore_post(post_id, reason, SRC_API, auth)

    if not can_view(post, user_id):  # a moderator who may not see the post gets an acknowledgement, not its content (D19)
        return {'post_view': post_removal_ack_view(post)}
    post_json = post_view(post=post, variant=4, user_id=user_id)
    return post_json


def post_post_mark_as_read(auth, data):
    if 'post_id' not in data and 'post_ids' not in data:
        raise Exception('post_id or post_ids required')

    user_id = authorise_api_user(auth)
    if 'post_id' in data:
        post_ids = [a_visible_post(data['post_id'], auth).id]
    else:
        # `else`, not `elif 'post_ids' in data`: the guard at the top of
        # this function already refused a request carrying neither. A batch
        # skips ids nobody holds or the caller may not see, so one of them
        # neither fails the rest nor tells the two cases apart.
        posts = without_hidden_here(Post.query.filter(Post.id.in_(data['post_ids']), visible_to_clause(Post, user_id)),
                                    Post, True)   # none hidden here (follow-up F6)
        post_ids = [post.id for post in posts]
    try:
        shared_post.mark_post_read(post_ids, data['read'], user_id)
    except IntegrityError:
        return {"success": False}
    return {"success": True}


def get_post_like_list(auth, data):
    post_id = data['post_id']
    page = data['page'] if 'page' in data else 1
    limit = data['limit'] if 'limit' in data else 50

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    user = authorise_api_user(auth, return_type='model')
    post = a_visible_post(post_id, auth)

    if post.community.is_moderator(user) or user_access('moderate all communities', user.id, on=post):
        banned_from_site_user_ids = list(db.session.execute(text('SELECT id FROM "user" WHERE banned = true')).scalars())
        banned_from_community_user_ids = list(db.session.execute(text
            ('SELECT user_id from "community_ban" WHERE community_id = :community_id'), {"community_id": post.community_id}).scalars())
        voters = without_local_only_actors(PostVote.query.join(User, User.id == PostVote.user_id), User, True)   # F6
        likes = voters.filter(PostVote.post_id == post_id, PostVote.effect != 0).order_by(PostVote.effect).order_by(
                  PostVote.created_at).paginate(page=page, per_page=limit, error_out=False)
        post_likes = []
        for like in likes:
            post_likes.append({
                'score': like.effect,
                'creator_banned_from_community': like.user_id in banned_from_community_user_ids,
                'creator_banned': like.user_id in banned_from_site_user_ids,
                'creator': user_view(user=like.user_id, variant=1, stub=True, user_id=user.id)
            })
        response_json = {
            'next_page': str(likes.next_num) if likes.next_num is not None else None,
            'post_likes': post_likes
        }
        return response_json
    else:
        raise Exception('Not a moderator')


def put_post_set_flair(auth, data):
    post_id = data['post_id']
    flair_list = data['flair_id_list'] if 'flair_id_list' in data else []

    post = a_visible_post(post_id, auth)
    user = authorise_api_user(auth, return_type='model')
    
    if post.community.is_moderator(user) or user_access('moderate all communities', user.id, on=post) or post.user_id == user.id:
        labels_before = post_labels(post)
        # Start by clearing the existing flair
        post.flair = []

        if flair_list:
            comm_flair = CommunityFlair.query.filter_by(community_id=post.community_id).all()
            flair_objs = [db.session.get(CommunityFlair, flair_id) for flair_id in flair_list]

            for flair in flair_objs:
                if flair in comm_flair:
                    # Flair from correct community, add it
                    post.flair.append(flair)

        db.session.commit()
        record_label_change(post, labels_before, user)

        if post.status == POST_STATUS_PUBLISHED:
            task_selector('edit_post', post_id=post.id)
        
        return post_view(post=post, variant=2, stub=False, user_id=user.id)
    else:
        raise Exception("Insufficient permissions")


def post_poll_vote(auth, data):
    post_id = a_visible_post(data['post_id'], auth).id
    choice_id = data['choice_id']

    user_id = authorise_api_user(auth)

    shared_post.vote_for_poll(post_id, choice_id, SRC_API, auth=auth)

    return {"post_view": post_view(post=post_id, variant=2, stub=False, user_id=user_id)}
