"""Post listings: the posts a viewer is shown for one listing request, as an ordered query.

One builder, `post_listing`, for every client API's list of posts. The alpha API's two listings use it today:
`get_post_list` (offset pages, with a raw-SQL fast path) and `get_post_list2` (keyset pages). Each filter is stated
once here with its raw-SQL spelling beside it, so the fast path and the query cannot drift apart again (D1227, D1228
and D1230 were each a filter one path had and the other did not).

The two alpha listings disagreed before they were merged (W0.1 step 2). Each disagreement is a field of `Spelling`,
and each caller passes its own, so neither changed; the owner rules on them later (the plan's W0.1 section lists
them). Paging stays with the caller: an offset page, a keyset bookmark and the boosted window are its business.
"""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, asc, desc, exists, or_, text

from app import db
from app.constants import POST_STATUS_REVIEWING, POST_TYPE_EVENT, POST_TYPE_POLL
# The modules, not the names: each reaches this file through app.activitypub before its function is defined (import
# cycles: feed.routes, topic.routes), as in app/api/alpha/utils/post.py
import app.feed.routes as feed_routes
import app.topic.routes as topic_routes
from app.feed_domains import shown_feed_communities
from app.local_only import found_here, hidden_here_sql, without_local_only
from app.models import Community, CommunityMember, Feed, FeedItem, Post, Topic, User, hidden_posts, read_posts, \
    utcnow
from app.reads.viewer import Restrictions
from app.storefront import BOOST_WINDOW, ON_DOMAIN_PARAM, boosted_top_sql, current_storefront, on_domain, \
    on_domain_sql
from app.utils import feed_readable_by, filtered_out_communities, recently_upvoted_posts, user_filters_home, \
    user_filters_posts
from app.visibility import listable_clause, listable_sql, visible_to_clause, visible_to_sql


@dataclass(frozen=True)
class Listing:
    """One request for a list of posts. `type` is the scope (All, Local, Popular, Subscribed, Moderating or
    ModeratorView); an All listing narrows to one community (by id or by name), a feed, a topic or an author. `nsfw` is
    '', 'Exclude', 'Only' or 'Include'; `query` searches the full text, or the url when `search_type` is 'Url'."""
    type: str = 'All'
    sort: str = 'Hot'
    community_id: int | None = None
    community_name: str | None = None
    feed_id: int | None = None
    topic_id: int | None = None
    person_id: int | None = None
    liked_only: bool = False
    saved_only: bool = False
    ignore_sticky: bool = False
    minimum_upvotes: int | None = None
    nsfw: str = ''
    query: str = ''
    search_type: str = 'Posts'


@dataclass(frozen=True)
class Spelling:
    """Where the alpha API's two listings disagreed when they were merged. Kept, not unified, for the owner to rule on;
    see the plan's W0.1 section for where each one came from."""
    raw_sql: bool                   # build the raw-SQL fast path where the request allows it
    languages_in_query: bool        # the viewer's read languages filter the query (the fast path always has them)
    hides_hidden_posts: bool        # the posts the viewer hid are left out (not while searching)
    hides_polls_and_events: bool    # polls and events are left out
    hides_read_when_searching: bool  # hide_read_posts holds for a search too
    new_sorts_by_prefix: bool       # any sort starting 'New' is New, not only 'New'
    active_needs_replies: bool      # Active lists only posts with a reply
    keyset_tiebreaks: bool          # New, Active and the named Top sorts order again with the id last


OFFSET_PAGES = Spelling(raw_sql=True, languages_in_query=False, hides_hidden_posts=True,
                        hides_polls_and_events=False, hides_read_when_searching=False, new_sorts_by_prefix=True,
                        active_needs_replies=False, keyset_tiebreaks=False)
"""get_post_list's."""

KEYSET_PAGES = Spelling(raw_sql=False, languages_in_query=True, hides_hidden_posts=False,
                        hides_polls_and_events=True, hides_read_when_searching=True, new_sorts_by_prefix=False,
                        active_needs_replies=True, keyset_tiebreaks=True)
"""get_post_list2's."""


TOP_WINDOWS = {'Top': timedelta(days=1), 'TopDay': timedelta(days=1), 'TopHour': timedelta(hours=1),
               'TopSixHour': timedelta(hours=6), 'TopTwelveHour': timedelta(hours=12), 'TopWeek': timedelta(days=7),
               'TopMonth': timedelta(days=28), 'TopThreeMonths': timedelta(days=90),
               'TopSixMonths': timedelta(days=180), 'TopNineMonths': timedelta(days=270),
               'TopYear': timedelta(days=365), 'TopAll': None}
"""How far back each Top sort looks; None is all time. Any other sort starting 'Top' looks back a day."""


@dataclass(frozen=True)
class RawListing:
    """The fast path's WHERE criteria (joined by AND), their parameters and its ORDER BY terms."""
    criteria: tuple[str, ...]
    parameters: dict
    order_by: tuple[str, ...]


@dataclass(frozen=True)
class PostListing:
    """What `post_listing` answers. `posts` is filtered and ordered; a boosted sort (`boosted`: its rank column and
    the columns that break its ties) is ordered by the caller, who knows the page, ahead of which go `sticky_columns`.
    `fast` is the raw-SQL fast path when the request allows one and the spelling asks for it, else None.
    `content_filters` are the viewer's keyword filters for the posts' views, `read_post_ids` the posts they read."""
    posts: object
    boosted: tuple | None
    sticky_columns: tuple
    content_filters: dict
    read_post_ids: tuple
    fast: RawListing | None


class _Builder:
    """The query and its raw-SQL twin, built side by side."""

    def __init__(self, spelling: Spelling):
        self.spelling = spelling
        self.posts = None
        self.criteria = []
        self.parameters = {}
        self.order_sql = []
        self.fast = True                # a narrowed listing, a full-text search or a liked/saved list turns it off
        self.instance_stickies = True   # instance stickies go first only on an unnarrowed listing

    def where(self, clause, sql: str | None = None, **parameters):
        self.posts = self.posts.filter(clause)
        self.sql(sql, **parameters)

    def sql(self, criterion: str | None, **parameters):
        if criterion:
            self.criteria.append(criterion)
        self.parameters.update(parameters)

    def narrowed(self):
        self.fast = False
        self.instance_stickies = False

    def order(self, clause, sql: str, tiebreak: bool):
        self.posts = self.posts.order_by(clause)
        self.order_sql.append(sql)
        if tiebreak and self.spelling.keyset_tiebreaks:
            self.posts = self.posts.order_by(clause, desc(Post.id))


def post_listing(user: User | None, restrictions: Restrictions, listing: Listing, spelling: Spelling,
                 named_community=None) -> PostListing:
    """The posts `user` (None: nobody signed in) is shown for `listing`, without what `restrictions` hides.

    `named_community` is the clause picking the community `listing.community_name` names (scoped by its ap_domain),
    which the caller builds: tests/test_local_name_lookups.py pins every name lookup to the function it is in."""
    b = _Builder(spelling)
    user_id = user.id if user else None
    signed_in = user_id is not None
    content_filters, by_author = _scope(b, user_id, restrictions, listing, named_community)
    profile_listing = by_author or bool(user and (listing.liked_only or listing.saved_only))
    _visible(b, user_id, signed_in, restrictions, profile_listing)
    if listing.query:
        b.instance_stickies = False
        if listing.search_type == 'Url':
            b.where(Post.url.ilike(f'%{listing.query}%'), 'p.url ILIKE :url_query', url_query=f'%{listing.query}%')
        else:
            b.fast = False
            b.posts = b.posts.search(listing.query, sort=listing.sort == 'Relevance').filter(Post.indexable == True)
    if user:
        read_post_ids = _viewer_filters(b, user, listing)
    else:
        read_post_ids = ()
        _nsfw(b, listing.nsfw)
    if listing.minimum_upvotes:
        # `Post.score`, not `Post.up_votes - Post.down_votes`: the score is what the raw SQL reads and every Top sort
        # orders by, so the same request cannot answer differently depending on which path served it (D1230)
        b.where(Post.score >= listing.minimum_upvotes, 'score >= :minimum_upvotes',
                minimum_upvotes=listing.minimum_upvotes)
    sticky_columns = ()
    if (listing.community_id or listing.community_name) and not listing.ignore_sticky:
        b.order(desc(Post.sticky), 'sticky DESC', tiebreak=False)
        sticky_columns += (Post.sticky,)
    if b.instance_stickies and not listing.ignore_sticky:
        b.order(desc(Post.instance_sticky), 'instance_sticky DESC', tiebreak=False)
        sticky_columns += (Post.instance_sticky,)
    boosted = _sort(b, listing.sort)
    if spelling.hides_polls_and_events:   # change when polls and events are supported
        b.posts = b.posts.filter(Post.type != POST_TYPE_POLL).filter(Post.type != POST_TYPE_EVENT)
    fast = RawListing(tuple(b.criteria), b.parameters, tuple(b.order_sql)) if spelling.raw_sql and b.fast else None
    return PostListing(b.posts, boosted, sticky_columns, content_filters, read_post_ids, fast)


def _scope(b: _Builder, user_id, restrictions: Restrictions, listing: Listing, named_community) -> tuple[dict, bool]:
    """The listing's scope: the base query, its blocks and its raw-SQL criteria. Answers the viewer's keyword filters
    for the views (none for a scope that has none), and whether it is an author's listing (a profile listing)."""
    r = restrictions
    # The fast path's blocks hold for every scope (but a domain block, for a moderator's listings); the query's are
    # each scope's own, below.
    if r.blocked_person_ids:
        b.sql('p.user_id NOT IN :blocked_person_ids', blocked_person_ids=tuple(r.blocked_person_ids))
    if r.blocked_community_ids:
        b.sql('community_id NOT IN :blocked_community_ids', blocked_community_ids=tuple(r.blocked_community_ids))
    if r.blocked_domain_ids and listing.type not in ('ModeratorView', 'Moderating'):
        b.sql('(p.domain_id is null OR p.domain_id NOT IN :blocked_domain_ids)',
              blocked_domain_ids=tuple(r.blocked_domain_ids))
    if r.blocked_instance_ids:
        b.sql('c.instance_id NOT IN :blocked_instance_ids AND p.instance_id NOT IN :blocked_instance_ids',
              blocked_instance_ids=tuple(r.blocked_instance_ids))
    if r.read_language_ids:
        b.sql('(p.language_id IN :read_language_ids OR p.language_id is null)',
              read_language_ids=tuple(r.read_language_ids))

    published = (Post.deleted == False, Post.status > POST_STATUS_REVIEWING)
    person = Post.user_id.not_in(r.blocked_person_ids)
    community = Post.community_id.not_in(r.blocked_community_ids)
    domain = or_(Post.domain_id == None, Post.domain_id.not_in(r.blocked_domain_ids))
    instance = Post.instance_id.not_in(r.blocked_instance_ids)
    community_instance = Community.instance_id.not_in(r.blocked_instance_ids)

    def in_communities(*clauses):
        """Every block, joined to the post's community and narrowed by `clauses`."""
        return Post.query.filter(*published, person, community, domain, instance). \
            join(Community, Community.id == Post.community_id).filter(*clauses, community_instance)

    def by_membership(blocks, *membership):
        return Post.query.filter(*published, *blocks). \
            join(CommunityMember, Post.community_id == CommunityMember.community_id). \
            filter(CommunityMember.user_id == user_id, *membership). \
            join(Community, Community.id == CommunityMember.community_id).filter(community_instance)

    content_filters, profile_listing = {}, False
    if listing.type == 'Local':
        storefront = current_storefront()
        b.posts = Post.query.filter(*published, person, domain, community). \
            join(Community, Community.id == Post.community_id).filter(Community.ap_id == None). \
            filter(on_domain(storefront, Community))
        b.sql(f'c.ap_id is null AND {on_domain_sql(storefront)}', **{ON_DOMAIN_PARAM: storefront.id})
    elif listing.type == 'Popular':
        b.posts = in_communities(Community.show_popular == True, Post.score > 100)
        b.sql('score > 100')
        b.sql('show_popular is true')
    elif listing.type == 'Subscribed' and user_id is not None:
        b.posts = by_membership((person, community, domain, instance), CommunityMember.is_banned == False)
        b.sql('community_id IN (SELECT community_id FROM community_member WHERE user_id = :user_id AND '
              'is_banned = FALSE)', user_id=user_id)
    elif listing.type in ('ModeratorView', 'Moderating') and user_id is not None:
        b.posts = by_membership((person, community, instance), CommunityMember.is_moderator == True)   # no domain block
        b.sql('community_id IN (SELECT community_id FROM community_member WHERE user_id = :user_id AND '
              'is_moderator = TRUE)', user_id=user_id)
    elif listing.community_name:
        b.narrowed()
        b.posts = in_communities(named_community)
        content_filters = user_filters_posts(user_id) if user_id else {}
    elif listing.community_id:
        b.narrowed()
        b.posts = in_communities(Community.id == listing.community_id)
        content_filters = user_filters_posts(user_id) if user_id else {}
    elif listing.feed_id:
        b.narrowed()
        b.posts = in_communities(Community.id.in_(_feed_community_ids(listing.feed_id, user_id)))
        content_filters = user_filters_posts(user_id) if user_id else {}
    elif listing.topic_id:
        b.narrowed()
        b.posts = in_communities(Community.id.in_(_topic_community_ids(listing.topic_id)))
        content_filters = user_filters_posts(user_id) if user_id else {}
    elif listing.person_id:
        b.narrowed()   # an author's posts, with no block on authors
        profile_listing = True
        b.posts = Post.query.filter(*published, community, domain, instance, Post.user_id == listing.person_id). \
            join(Community, Community.id == Post.community_id).filter(community_instance)
    else:
        b.posts = in_communities(Community.show_all == True)
        b.sql('show_all is true')
        content_filters = user_filters_home(user_id) if user_id else {}
    return content_filters, profile_listing


def _feed_community_ids(feed_id: int, user_id) -> list[int]:
    """The communities of the feed `feed_id`, and of its children when it shows their posts."""
    feed = found_here(Feed, feed_id)
    # D1394: an id this caller may not read is not an id it may list by; `get_feed` refuses a private feed to
    # anybody but its owner, and so does this
    if feed is None or not feed_readable_by(feed, user_id):
        raise Exception('feed not found')
    feed_ids = feed_routes.get_all_child_feed_ids(feed) if feed.show_posts_in_children else [feed.id]
    community_ids = []
    for fid in feed_ids:
        # filter_by, not `join(Feed, FeedItem.feed_id == fid)`: that join names no predicate between the two tables
        # and fetched each row once per feed on the instance (D1394)
        items = FeedItem.query.filter_by(feed_id=fid).join(Community, Community.id == FeedItem.community_id)
        for item in shown_feed_communities(items, user_id is not None):   # one hidden here lists no posts
            community_ids.append(item.community_id)
    return community_ids


def _topic_community_ids(topic_id: int) -> list[int]:
    """The communities of the topic `topic_id`, and of its children when it shows their posts."""
    topic = db.session.get(Topic, topic_id)
    if not topic:
        raise Exception('topic not found')
    topic_ids = topic_routes.get_all_child_topic_ids(topic) if topic.show_posts_in_children else [topic.id]
    return [community.id for tid in topic_ids for community in Community.query.filter(Community.topic_id == tid)]


def _visible(b: _Builder, user_id, signed_in: bool, restrictions: Restrictions, profile_listing: bool):
    """Private communities, post visibility and local-only posts."""
    private_ids = restrictions.private_community_ids
    b.posts = b.posts.filter(or_(Community.private == False, Community.id.in_(private_ids)))
    # Always a private-community criterion, member of one or not: an anonymous caller is a member of none, and when
    # the fast path left it out they were handed every private community's posts (D1227)
    if private_ids:
        b.sql('(c.private is false OR community_id IN :private_community_ids)',
              private_community_ids=tuple(private_ids))
    else:
        b.sql('c.private is false')
    # a listing shows public posts only (interop D7); the profile listings (by person, saved, liked) apply the
    # viewer predicate instead
    if profile_listing:
        b.where(visible_to_clause(Post, user_id), visible_to_sql('p'), visibility_viewer_id=user_id)
    else:
        b.where(listable_clause(Post), listable_sql('p'))
    b.posts = without_local_only(b.posts, Post, visible=signed_in)   # spec §3.4
    # the raw SQL's spelling of without_local_only, asked only of a caller with a fast path (it reads the database)
    if b.spelling.raw_sql and (hidden := hidden_here_sql('p', Post, signed_in)) != 'false':
        b.sql(f'NOT {hidden}')
    if b.spelling.languages_in_query and restrictions.read_language_ids:
        b.posts = b.posts.filter(or_(Post.language_id.in_(restrictions.read_language_ids), Post.language_id == None))


def _viewer_filters(b: _Builder, user: User, listing: Listing) -> tuple:
    """A signed-in viewer's own lists and settings. Answers the ids of the posts they read (none for a liked or saved
    list, which shows them all)."""
    user_id = user.id
    read_post_ids = ()
    if listing.liked_only:
        b.narrowed()
        b.posts = b.posts.filter(Post.id.in_(recently_upvoted_posts(user_id)), Post.user_id != user_id)
    elif listing.saved_only:
        b.narrowed()
        saved = tuple(db.session.execute(text('SELECT post_id FROM "post_bookmark" WHERE user_id = :user_id'),
                                         {'user_id': user_id}).scalars())
        b.posts = b.posts.filter(Post.id.in_(saved))
    else:
        read_post_ids = tuple(db.session.execute(
            text('SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id'), {'user_id': user_id}).scalars())
        if user.ignore_bots == 1:
            b.where(Post.from_bot == False, 'from_bot = false')
        if user.ignore_reposters == 1:
            b.where(Post.from_reposter == False, 'from_reposter = false')
        if user.hide_nsfl == 1:
            b.where(Post.nsfl == False, 'p.nsfl = false')
        if listing.nsfw == '' and user.hide_nsfw == 1:
            b.where(Post.nsfw == False, 'p.nsfw = false')
        else:
            _nsfw(b, listing.nsfw, default='Include')
        if user.hide_gen_ai == 1:
            b.where(Post.ai_generated == False, 'p.ai_generated = false')
        if user.hide_read_posts and (not listing.query or b.spelling.hides_read_when_searching):
            rp = read_posts.alias()
            b.posts = b.posts.filter(~exists().where(and_(rp.c.user_id == user_id, rp.c.read_post_id == Post.id)))
            if read_post_ids:
                b.sql('p.id NOT IN :read_post_ids', read_post_ids=read_post_ids)
        if b.spelling.hides_hidden_posts and not listing.query:
            hidden_ids = tuple(db.session.execute(
                text('SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id'),
                {'user_id': user_id}).scalars())
            if hidden_ids:
                hp = hidden_posts.alias()
                b.where(~exists().where(and_(hp.c.user_id == user_id, hp.c.hidden_post_id == Post.id)),
                        'p.id NOT IN :hidden_post_ids', hidden_post_ids=hidden_ids)
    filtered_out = filtered_out_communities(user)
    if filtered_out:
        b.where(Post.community_id.not_in(filtered_out), 'community_id NOT IN :filtered_out_community_ids',
                filtered_out_community_ids=tuple(filtered_out))
    return read_post_ids


def _nsfw(b: _Builder, nsfw: str, default: str = 'Exclude'):
    """The request's NSFW choice. Asking nothing ('') means `default`: Exclude for nobody signed in (a fresh client's
    first listing), Include for a viewer, whose own hide_nsfw setting has already had its say."""
    if (nsfw or default) == 'Exclude':
        b.where(Post.nsfw == False, 'p.nsfw = false')
    elif nsfw == 'Only':
        b.where(Post.nsfw == True, 'p.nsfw = true')


def _sort(b: _Builder, sort: str) -> tuple | None:
    """Orders by `sort`. A boosted sort (Hot, Scaled) is left for the caller to order and answers (rank column, the
    columns that break its ties); Relevance is the full-text search's own order; an unknown sort orders nothing."""
    if sort == 'Hot':
        return Post.ranking, (Post.posted_at,)
    if sort.startswith('Top'):
        window = TOP_WINDOWS.get(sort, timedelta(days=1))
        if window is not None:
            since = utcnow() - window
            b.where(Post.posted_at > since, 'posted_at > :top_threshold', top_threshold=since)
        b.order(desc(Post.score), 'score DESC', tiebreak=sort in TOP_WINDOWS)
    elif sort == 'New' or (b.spelling.new_sorts_by_prefix and sort.startswith('New')):
        b.order(desc(Post.posted_at), 'posted_at DESC', tiebreak=True)
    elif sort == 'Scaled':
        b.where(Post.ranking_scaled != None, 'ranking_scaled IS NOT NULL')
        b.where(Post.from_bot == False, 'from_bot = false')
        return Post.ranking_scaled, (Post.ranking, Post.posted_at)
    elif sort == 'Active':
        b.where(Post.last_active != None, 'p.last_active is not null')
        if b.spelling.active_needs_replies:
            b.where(Post.reply_count > 0)
        b.order(desc(Post.last_active), 'p.last_active DESC', tiebreak=True)
    elif sort.startswith('Old'):
        b.order(asc(Post.posted_at), 'posted_at ASC', tiebreak=False)
    return None


FAST_COLUMNS = """p.id as post_id,
                    p.user_id, p.domain_id, p.instance_id,
                    p.status, p.deleted, p.deleted_by,
                    p.type, p.nsfw, p.nsfl, p.sticky, p.instance_sticky, p.ai_generated,
                    p.from_bot, p.created_at, p.posted_at, p.last_active,
                    p.up_votes, p.down_votes, p.ranking, p.ranking_scaled, p.score,
                    p.reply_count, p.language_id,
                    c.id as community_id, c.show_all, c.show_popular, c.instance_id as community_instance_id,
                    c.nsfw as community_nsfw, c.banned as community_banned,
                    c.private, c.ap_id as community_ap_id, c.name as community_name, c.ap_domain"""


def fast_post_ids(listing: PostListing) -> list[int]:
    """The ids of the first thousand posts of `listing`, in order, through its raw-SQL fast path."""
    raw = listing.fast
    parameters = dict(raw.parameters)
    from_where = 'FROM post p JOIN community c ON c.id = p.community_id WHERE p.deleted = FALSE AND p.status > 0 AND ' \
                 'c.banned = FALSE AND ' + ' AND '.join(raw.criteria)
    if listing.boosted:     # read tier by tier, so that ix_post_feed_hot still serves each read
        rank, tail = listing.boosted
        sql, boost_parameters = boosted_top_sql('p.id AS post_id', 't.post_id', from_where, rank.key,
                                                tuple(column.key for column in tail), BOOST_WINDOW,
                                                tuple(column.key for column in listing.sticky_columns))
        parameters.update(boost_parameters)
    else:
        sql = f'SELECT {FAST_COLUMNS} {from_where}'
        # Only when there is something to order by: Relevance and an unknown sort order nothing, and with the
        # stickies ignored the query read `ORDER BY  LIMIT 1000` (D1225)
        if raw.order_by:
            sql += ' ORDER BY ' + ', '.join(raw.order_by)
        sql += ' LIMIT 1000'
    return db.session.execute(text(sql), parameters).scalars().all()
