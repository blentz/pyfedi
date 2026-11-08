"""Replies: a post's reply tree, and the comment listings, as a viewer may see them.

Two reads, each with the callers it has today:
- `thread_replies` and `reply_tree`/`reply_branch`: a post's thread as a tree, which `app.post.util.post_replies` and
  `get_comment_branch` build (the post page, and the alpha API's `get_post_replies`, read it through them);
- `comment_listing`: the alpha API's `get_reply_list`, a flat listing (by liked, saved, search, author, community, or
  everything) or a conversation (a post's or a reply's thread, a page at a time).

They disagreed before they were moved here (W0.1 step 3). Each disagreement is named: a `ThreadSpelling` for the tree's
two reads, a `BlockSpelling` for how each read leaves out what the viewer blocked, and the two sort functions; the plan's
W0.1 section lists them for the owner to rule on. Views and offset paging stay with the caller; a depth-first
conversation is cut into pages here, because which rows make a page is decided by the tree, not by an offset.
"""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Float, and_, any_, asc, cast, desc, exists, func, or_, text
from sqlalchemy.orm import aliased

from app import db
from app.local_only import found_here, without_local_only
from app.models import Community, Post, PostReply, User, utcnow
import app.reads.post as reads_post   # a module, looked up when called: app.reads.post's own imports lead back here
from app.reads.viewer import Restrictions
from app.storefront import current_storefront, domain_base_url, domain_rows, on_domain
from app.permissions import user_access
from app.utils import joined_communities, moderating_communities_ids
from app.visibility import listable_clause, visible_to_clause


@dataclass(frozen=True)
class BlockSpelling:
    """How a read leaves out the replies of people and instances the viewer blocked."""
    takes_descendants: bool         # a blocked reply's own replies go with it, in the query (else only the reply)
    spares_unknown_instance: bool   # a reply with no instance survives an instance block


TREE_BLOCKS = BlockSpelling(takes_descendants=False, spares_unknown_instance=True)
"""post_replies' and get_comment_branch's: a blocked reply's own replies fall out of the tree with it (a reply whose
parent is not in the rows is not placed)."""

LISTING_BLOCKS = BlockSpelling(takes_descendants=False, spares_unknown_instance=False)
"""get_reply_list's flat listing: a listing has no tree to leave gaps in."""

CONVERSATION_BLOCKS = BlockSpelling(takes_descendants=True, spares_unknown_instance=False)
"""get_reply_list's conversation: rows are paged before any tree is built, so a blocked reply's replies are left out
by their path."""


@dataclass(frozen=True)
class ThreadSpelling:
    """Where a post's whole thread (post_replies) and one branch of it (get_comment_branch) disagreed."""
    hide_threshold_spares_uncollapsible: bool   # the viewer's reply_hide_threshold spares a reply not collapsible
    limit: int | None                           # read at most this many replies


WHOLE_THREAD = ThreadSpelling(hide_threshold_spares_uncollapsible=True, limit=2000)
"""post_replies': paginating indented replies is too hard, so it reads the first 2000."""

ONE_BRANCH = ThreadSpelling(hide_threshold_spares_uncollapsible=False, limit=None)
"""get_comment_branch's."""


def _without_blocked(replies, restrictions: Restrictions, spelling: BlockSpelling):
    """`replies` without those by people and instances `restrictions` blocks."""
    if person_ids := restrictions.blocked_person_ids:
        if spelling.takes_descendants:
            parent = aliased(PostReply)
            replies = replies.filter(~or_(PostReply.user_id.in_(person_ids),
                                          exists().where(and_(parent.id == any_(PostReply.path),
                                                              parent.user_id.in_(person_ids)))))
        else:
            replies = replies.filter(PostReply.user_id.not_in(person_ids))
    if instance_ids := restrictions.blocked_instance_ids:
        if spelling.takes_descendants:
            parent = aliased(PostReply)
            replies = replies.filter(~or_(PostReply.instance_id.in_(instance_ids),
                                          exists().where(and_(parent.id == any_(PostReply.path),
                                                              parent.instance_id.in_(instance_ids)))))
        elif spelling.spares_unknown_instance:
            replies = replies.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        else:
            replies = replies.filter(PostReply.instance_id.not_in(instance_ids))
    return replies


# ------------------------------------------------------------------ a post's thread, as a tree

def thread_replies(post: Post, viewer: User | None, restrictions: Restrictions, sort_by: str,
                   spelling: ThreadSpelling, silenced_instance_ids=(), leave_out_hidden_here: bool = True):
    """The replies of `post` that `viewer` (None: nobody signed in) is shown in its thread, ordered by `sort_by`
    ('hot', 'top', 'new' or 'old'; anything else orders nothing).

    A signed-in viewer loses what `restrictions` blocks, bots' replies when they ignore bots or reposters, replies
    under their reply_hide_threshold (unless they moderate the post) and replies in languages they do not read. An
    anonymous reader loses `silenced_instance_ids`' replies (the caller reads them: post_replies asks
    `silenced_instances`, get_comment_branch none). A reply hidden here goes, and all its replies with it (spec §3.4),
    unless `leave_out_hidden_here` is False (an archive reads every reply)."""
    comments = db.session.query(PostReply).filter_by(post_id=post.id)
    if viewer:
        comments = _without_blocked(comments, restrictions, TREE_BLOCKS)
        if viewer.ignore_bots == 1 or viewer.ignore_reposters == 1:
            comments = comments.filter(PostReply.from_bot == False)
        # `is_moderator(viewer)`, not `is_moderator()`. With no argument the method falls back to
        # `current_user.get_id()` -- the WEB session -- which is not who `viewer` is when the API calls this: every
        # authenticated API reader was treated as a non-moderator and had the hide threshold applied regardless, and
        # with no request context at all (a direct call, as the tests make) `current_user` is None and the line is an
        # AttributeError. The web callers pass `current_user` as `viewer`, so naming it changes nothing for them.
        if viewer.reply_hide_threshold and not (user_access('moderate all communities', viewer.id, on=post) or
                                                post.community.is_moderator(viewer)):
            if spelling.hide_threshold_spares_uncollapsible:
                comments = comments.filter(or_(PostReply.score > viewer.reply_hide_threshold,
                                               PostReply.collapsible == False))
            else:
                comments = comments.filter(PostReply.score > viewer.reply_hide_threshold)
        if viewer.read_language_ids and len(viewer.read_language_ids) > 0:
            comments = comments.filter(
                or_(PostReply.language_id.in_(tuple(viewer.read_language_ids)), PostReply.language_id == None))
    elif silenced_instance_ids:
        comments = comments.filter(or_(PostReply.instance_id.not_in(silenced_instance_ids),
                                       PostReply.instance_id == None))
    if leave_out_hidden_here:
        comments = without_local_only(comments, PostReply, viewer is not None)
    comments = _tree_order(comments, sort_by)
    if spelling.limit is not None:
        comments = comments.limit(spelling.limit)
    return comments


def _tree_order(comments, sort_by: str):
    if sort_by == 'hot':
        return comments.order_by(desc(PostReply.ranking))
    if sort_by == 'top':
        return comments.order_by(desc(PostReply.score))
    if sort_by == 'new':
        return comments.order_by(desc(PostReply.posted_at))
    if sort_by == 'old':
        return comments.order_by(asc(PostReply.posted_at))
    return comments


def _entries(rows) -> dict:
    """{id: {'comment': reply, 'replies': [its entries]}} for `rows`, in their order; a reply whose parent is not in
    `rows` is placed under nothing."""
    entries = {row.id: {'comment': row, 'replies': []} for row in rows}
    for row in rows:
        if row.parent_id is not None:
            parent = entries.get(row.parent_id)
            if parent:
                parent['replies'].append(entries[row.id])
    return entries


def reply_tree(rows) -> list[dict]:
    """The top-level replies of `rows`, each with its replies beneath it."""
    return [entry for entry in _entries(rows).values() if entry['comment'].parent_id is None]


def reply_branch(rows, comment_id: int) -> list[dict]:
    """The branch of `rows` whose root is `comment_id`: [] when it is not among them."""
    return [entry for entry in _entries(rows).values() if entry['comment'].id == comment_id]


# ------------------------------------------------------------------ the comment listings

@dataclass(frozen=True)
class CommentListing:
    """One request for a list of comments. Narrowed by liked, saved, a search `query`, an author or a community, it is
    a listing, which `post_id` narrows further and `type` (All, Local, Subscribed, Moderating or ModeratorView) scopes.
    Narrowed by none of those, a `post_id` or `parent_id` asks for a conversation, and nothing at all for every
    comment. `max_depth` is below it in a listing, at most it (under the parent) in a conversation; `depth_first` asks a
    conversation for whole branches a page at a time, which `page` and `limit` cut."""
    type: str = 'All'
    sort: str = 'New'
    query: str | None = None
    liked_only: bool = False
    saved_only: bool = False
    person_id: int | None = None
    community_id: int | None = None
    post_id: int | None = None
    parent_id: int | None = None
    max_depth: int | None = None
    depth_first: bool = False
    page: int = 1
    limit: int = 10


@dataclass(frozen=True)
class CommentRows:
    """What `comment_listing` answers. `replies` is filtered and ordered. `conversation` is whether it is a thread rather
    than a listing. `one_page` is whether the rows are already one depth-first page, `next_page` then naming the next
    one (None: the last); otherwise the caller pages `replies`."""
    replies: object
    conversation: bool
    one_page: bool
    next_page: str | None


def comment_listing(user_id: int | None, restrictions: Restrictions, listing: CommentListing,
                    liked_reply_ids=(), saved_reply_ids=()) -> CommentRows:
    """The comments `user_id` (None: nobody signed in) is shown for `listing`, without what `restrictions` hides (the
    caller exempts the viewer's own profile listing). `liked_reply_ids` and `saved_reply_ids` are the viewer's upvoted
    and bookmarked replies, for a liked or saved listing.

    Refusals are the alpha API's texts: a liked or saved listing needs a viewer, so do Subscribed, Moderating and
    ModeratorView; an anonymous reader may not page past the 10,000th of every comment ('unknown', deliberately vague);
    a parent nobody holds, or hidden here, is 'Comment with parent_id not found.'."""
    # A private community's comments are its members' (refuse_private_thread): asked for by its community, its post or
    # a reply in it, the request is refused; a wider listing leaves them out (_without_unjoined_private)
    if listing.community_id is not None:
        reads_post.refuse_private_thread(db.session.get(Community, listing.community_id), user_id)
    if listing.post_id is not None and (post := db.session.get(Post, listing.post_id)) is not None:
        reads_post.refuse_private_thread(post.community, user_id)
    if listing.parent_id is not None and (parent := db.session.get(PostReply, listing.parent_id)) is not None:
        reads_post.refuse_private_thread(db.session.get(Community, parent.community_id), user_id)

    replies = None
    if listing.liked_only:
        if not user_id:
            raise Exception('Login required for liked_only query')
        replies = PostReply.query.filter(PostReply.id.in_(liked_reply_ids), PostReply.user_id != user_id)
    if listing.saved_only:
        if not user_id:
            raise Exception('Login required for saved_only query')
        if not replies:
            replies = PostReply.query.filter(PostReply.id.in_(saved_reply_ids))
        else:
            replies = replies.filter(PostReply.id.in_(saved_reply_ids))
    if listing.query:
        if not replies:
            replies = PostReply.query.search(listing.query, sort=listing.sort == 'Relevance')
        else:
            replies = replies.search(listing.query, sort=listing.sort == 'Relevance')
        replies = replies.filter(PostReply.indexable == True)
    if listing.person_id is not None:
        if not replies:
            replies = PostReply.query.filter_by(user_id=listing.person_id)
        else:
            replies = replies.filter_by(user_id=listing.person_id)
    if listing.community_id is not None:
        if not replies:
            replies = PostReply.query.filter_by(community_id=listing.community_id)
        else:
            replies = replies.filter_by(community_id=listing.community_id)
    if not replies and listing.post_id is None and listing.parent_id is None:
        replies = PostReply.query
        if user_id is None and listing.page * listing.limit > 10000:
            raise Exception('unknown')   # deliberately vague response

    one_page, next_page = False, None
    if replies:
        conversation = False
        replies = _listing(replies, user_id, restrictions, listing)
    else:
        conversation = True
        replies, one_page, next_page = _conversation(restrictions, listing)

    # A listing or a conversation alike: a local-only reply's own replies are local-only too, so leaving them out makes
    # no gap in a thread (spec §3.4)
    replies = without_local_only(replies, PostReply, visible=user_id is not None)
    return CommentRows(_listing_order(replies, listing.sort), conversation, one_page, next_page)


def _listing(replies, user_id, restrictions: Restrictions, listing: CommentListing):
    """A flat listing's scope, visibility and blocks."""
    # D1199: the scope applies to a listing narrowed by an author, a community or nothing at all too. It does not apply
    # to a conversation: a conversation is not a listing.
    if listing.type == 'Local':
        replies = replies.filter(or_(PostReply.ap_id == None, *[PostReply.ap_id.startswith(domain_base_url(row) + '/')
                                                                 for row in domain_rows()]))
        # a reply by any hosted domain's account is stored under the default domain's base URL (object ids on the
        # author's own domain are not shipped), so it is local when it is on any hosted base; and its author belongs to
        # this domain, wherever it was posted
        replies = replies.filter(PostReply.user_id.in_(
            db.session.query(User.id).filter(User.ap_id == None, on_domain(current_storefront()))))
    elif listing.type == 'Moderating' or listing.type == 'ModeratorView':
        if user_id:
            replies = replies.filter(PostReply.community_id.in_(moderating_communities_ids(user_id=user_id)))
        else:
            raise Exception('incorrect login')
    elif listing.type == 'Subscribed':
        if user_id:
            comm_ids = [comm.id for comm in joined_communities(user_id=user_id)]
            comm_ids.extend(moderating_communities_ids(user_id=user_id))
            replies = replies.filter(PostReply.community_id.in_(comm_ids))
        else:
            raise Exception('incorrect login')

    # a listing omits a hidden reply outright (interop D7); only a threaded conversation keeps a stub, to hold the tree
    # together (D18). The person, saved and liked listings are the profile surfaces: they show what the viewer may see
    # and omit the rest, with no stub.
    if listing.person_id is not None or listing.saved_only or listing.liked_only:
        replies = replies.filter(visible_to_clause(PostReply, user_id))
    else:
        replies = replies.filter(listable_clause(PostReply))
    # safe to just remove any replies by blocked users (won't cause gaps in threaded convo)
    replies = _without_blocked(replies, restrictions, LISTING_BLOCKS)
    replies = _without_unjoined_private(replies, user_id)
    if listing.post_id is not None:   # a further narrowing of liked_only, saved_only, person_id or community_id
        replies = replies.filter_by(post_id=listing.post_id)
    if listing.max_depth is not None:
        replies = replies.filter(PostReply.depth < listing.max_depth)
    return replies


def _without_unjoined_private(replies, user_id):
    """Leave out the comments of every private community the viewer is not a member of (all of them for nobody signed
    in). Membership is the viewer's own: their own profile listing, exempt from their blocks, still shows their comments
    in the private communities they belong to."""
    member_of = Restrictions(user_id).private_community_ids
    unjoined = db.session.query(Community.id).filter(Community.private == True)
    if member_of:
        unjoined = unjoined.filter(Community.id.not_in(member_of))
    return replies.filter(PostReply.community_id.not_in(unjoined))


def _conversation(restrictions: Restrictions, listing: CommentListing):
    """A post's or a reply's thread: (replies, one_page, next_page). Blocked people's and instances' replies go with
    all their replies; nothing else is left out, to avoid creating gaps."""
    parent_id = None   # stays None for a post_id query
    max_depth = None
    if listing.parent_id is not None:
        parent_id = listing.parent_id
        parent = found_here(PostReply, parent_id)   # hidden here or nobody's: one answer (follow-up F6, round 4)
        if parent is None:
            raise Exception('Comment with parent_id not found.')
        parent_depth = parent.depth
        if listing.max_depth is not None:
            max_depth = parent_depth + listing.max_depth
    else:
        post_id = listing.post_id
        max_depth = listing.max_depth

    one_page, next_page = False, None
    # some apps paginate through all replies to get them all in one session and build the tree client-side;
    # replies.paginate() serves those (it won't matter if the parent of a reply on page 1 is on page 2). Others render
    # each page as they get it, so each page needs the complete info (no missing parents), and a page may then exceed
    # its limit: 'depth_first' (although they're likely better off using the /post/replies route). Note: ordering by
    # depth, posted_at gives boring results (all depth=0 on page 1)
    if listing.depth_first:
        one_page = True
        # Interpolated rather than bound, as they always were: each is an int (the request schema's, or the parent's)
        if parent_id is not None:
            where_query = f' WHERE root_id = {parent_id}' if parent_depth == 0 else \
                f' WHERE path @> ARRAY[{parent_id}]'
        else:
            where_query = f' WHERE post_id = {post_id}'
        # D1198: this lacked its f-prefix, so the braces themselves reached Postgres
        depth_query = f' AND depth <= {max_depth}' if max_depth is not None else ''
        if listing.sort == 'Hot':
            sort_query = ' ORDER BY ranking DESC, posted_at DESC'
        elif listing.sort == 'Top':
            sort_query = ' ORDER BY up_votes - down_votes DESC'
        elif listing.sort == 'Old':
            sort_query = ' ORDER BY posted_at'
        else:
            sort_query = ' ORDER BY posted_at DESC'
        reply_paths = db.session.execute(text('SELECT path from "post_reply"' + where_query + depth_query +
                                              sort_query)).scalars()
        processed = {0}
        page_array = [None, []]   # not really an array, I know
        page_index = 1
        for reply_path in reply_paths:
            for element in reply_path:
                # element == 0 means new branch of comments, not dependent on this branch
                if element == 0 and len(page_array[page_index]) >= listing.limit:
                    page_index += 1
                    page_array.append([])
                if element not in processed:
                    page_array[page_index].append(element)
                processed.add(element)
        page = listing.page
        replies = PostReply.query.filter(PostReply.id.in_(page_array[page]))
        if page_index > page and len(page_array[page + 1]) > 0:
            next_page = str(page + 1)
    else:
        if parent_id is not None:
            if parent_depth == 0:
                replies = PostReply.query.filter_by(root_id=parent_id)
            else:
                reply_ids = db.session.execute(text('SELECT id FROM "post_reply" WHERE path @> ARRAY[:id]'),
                                               {"id": parent_id}).scalars()
                replies = PostReply.query.filter(PostReply.id.in_(reply_ids))
        else:
            replies = PostReply.query.filter_by(post_id=post_id)
        if max_depth is not None:
            replies = replies.filter(PostReply.depth <= max_depth)

    # blocked people's and instances' comments go, and all their replies: get_comment_branch() isn't used here, to
    # avoid creating gaps
    return _without_blocked(replies, restrictions, CONVERSATION_BLOCKS), one_page, next_page


TOP_WINDOWS = {'TopHour': timedelta(hours=1), 'TopSixHour': timedelta(hours=6), 'TopTwelveHour': timedelta(hours=12),
               'TopDay': timedelta(days=1), 'TopWeek': timedelta(days=7), 'TopMonth': timedelta(days=28),
               'TopThreeMonths': timedelta(days=90), 'TopSixMonths': timedelta(days=180),
               'TopNineMonths': timedelta(days=270), 'TopYear': timedelta(days=365)}
"""How far back each windowed Top sort of a comment listing looks; Top and TopAll look back for ever."""


def _listing_order(replies, sort: str):
    """A comment listing's order. Relevance is the search's own; an unknown sort is New."""
    net = desc(PostReply.up_votes - PostReply.down_votes)
    if sort == 'Hot' or sort == 'Scaled':
        return replies.order_by(desc(PostReply.ranking)).order_by(desc(PostReply.posted_at))
    if sort == 'Top' or sort == 'TopAll':
        return replies.order_by(net)
    if sort in TOP_WINDOWS:
        return replies.filter(PostReply.posted_at > utcnow() - TOP_WINDOWS[sort]).order_by(net)
    if sort == 'Old':
        return replies.order_by(PostReply.posted_at)
    if sort == 'Relevance':
        return replies   # already done as part of the search query
    if sort == 'Controversial':
        # Pulled from the reddit algorithm:
        # https://github.com/reddit-archive/reddit/blob/753b17407e9a9dca09558526805922de24133d53/r2/r2/lib/db/_sorts.pyx#L60
        # D1200: the divisor is `greatest(up, down, 1)`; `coalesce(greatest(up, down), 1)` guarded NULL and not zero,
        # and a comment with no votes divided by it
        up, down = func.coalesce(PostReply.up_votes, 0), func.coalesce(PostReply.down_votes, 0)
        return replies.order_by(desc(func.coalesce(func.pow(
            up * down, cast(func.least(up, down), Float) / cast(func.greatest(up, down, 1), Float)), 0)))
    return replies.order_by(desc(PostReply.posted_at))   # New, Active and any other
