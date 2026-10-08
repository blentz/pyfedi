import hmac
import os.path
import json
import time
from datetime import timedelta, timezone
from random import randint

import flask
from feedgen.feed import FeedGenerator
from furl import furl
from markupsafe import Markup, escape
from pyld import jsonld
from sqlalchemy import or_, and_, func
from sqlalchemy.orm import joinedload
from ua_parser import parse as uaparse

from app import db, cache, limiter, plugins
from app.activitypub.util import users_total, active_month, local_posts, local_communities, \
    lemmy_site_data, is_activitypub_request, find_microblogging_community
from app.discovery import KIND_COMMUNITY
from app.discovery import MEDIA_SOFTWARE
from app.discovery.media import MEDIA_COMMUNITY_SQL, platform_community_clause
from app.discovery.search import discovery_fallback
from app.activitypub.signature import default_context, LDSignature, HttpSignature
from app.admin.util import topics_for_form
from app.api.alpha.utils.misc import get_resolve_object
from app.constants import SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_MODERATOR, \
    POST_STATUS_REVIEWING
from app.email import send_email, send_registration_approved_email
from app.inoculation import inoculation
from app.community.live import HOME_LIVE_FILTERS, LIVE_LIMIT, LIVE_WINDOW, home_live_available, home_live_key
from app.main import bp
from flask import g, flash, request, current_app, url_for, redirect, make_response, jsonify, send_file, abort
from flask_login import current_user
from flask_babel import _
from sqlalchemy import desc, text

from app.main.forms import ShareLinkForm
from app.main.util import sidebar_active_communities, sidebar_new_instances, sidebar_upcoming_events, \
    sidebar_new_communities, _base_list_communities_context, reload_url
from app.translation import LibreTranslateAPI
from app.visibility import listable_clause, modlog_open_clause, visible_to_clause
from app.utils import render_template, ensure_rss_token, get_setting, request_etag_matches, return_304, blocked_domains, rss_token_user, \
    ap_datetime, shorten_string, user_filters_home, \
    joined_communities, moderating_communities, markdown_to_html, \
    blocked_or_banned_instances, communities_banned_from, topic_tree, recently_upvoted_posts, recently_downvoted_posts, \
    menu_topics, blocked_communities, \
    permission_required, debug_mode_only, ip_address, menu_instance_feeds, menu_my_feeds, menu_subscribed_feeds, \
    feed_tree_public, gibberish, get_deduped_post_ids, paginate_post_ids, post_ids_to_models, html_to_text, \
    get_redis_connection, subscribed_feeds, joined_or_modding_communities, login_required_if_private_instance, \
    retrieve_image_hash, possible_communities, remove_tracking_from_link, reported_posts, \
    moderating_communities_ids, user_notes, login_required, safe_order_by, filtered_out_communities, \
    num_topics, referrer, block_honey_pot, user_pronouns, get_instance_stickies, \
    community_membership_private, favorite_communities, mimetype_from_url, feed_entry_body, check_anoobis, \
    is_safe_redirect_target, feed_readable_by, refuse_if_private_instance
from app.models import Community, CommunityMember, Post, Site, User, utcnow, Topic, Instance, \
    Notification, Language, community_language, ModLog, Feed, FeedItem, CmsPage, BannedInstances, BotChallenge
from app.ldap_utils import test_ldap_connection, sync_user_to_ldap, login_with_ldap
import boto3
from app.activitypub.routes import replay_inbox_request
import app as app_pkg


@bp.route('/', methods=['HEAD', 'GET'])
@bp.route('/home', methods=['GET'])
@bp.route('/home/<sort>', methods=['GET'])
@bp.route('/home/<sort>/<view_filter>', methods=['GET'])
@login_required_if_private_instance
def index(sort=None, view_filter=None):
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()

    if sort is None:
        sort = current_user.default_sort if current_user.is_authenticated else 'hot'

    if view_filter is None:
        view_filter = current_user.default_filter if current_user.is_authenticated else g.site.default_filter
        # anonymous users cannot use "subscribed"
        if current_user.is_anonymous and view_filter == 'subscribed':
            view_filter = 'popular'
        if view_filter is None:
            view_filter = 'subscribed' if current_user.is_authenticated else 'popular'

    page = request.args.get('page', 0, type=int)
    tag = request.args.get('tag', '')
    live = sort == 'live' and home_live_available(current_user, view_filter, page, tag)
    if sort == 'live' and not live:
        sort = 'new'

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"{sort}_{view_filter}_{hash(str(g.site.last_active))}"
    if current_user.is_anonymous and request_etag_matches(current_etag):
        return return_304(current_etag)

    verification_warning()
    block_honey_pot()

    return home_page(sort, view_filter,
                     page=page,
                     result_id=request.args.get('result_id', gibberish(15)) if current_user.is_authenticated else None,
                     low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
                     tag=tag, live=live)


@bp.route('/home/live_posts/<view_filter>', methods=['GET'])
@login_required
@limiter.limit('12/minute', key_func=lambda: f'live:{current_user.id}:{request.view_args["view_filter"]}')
def home_live_posts(view_filter):
    """New teasers for a home tab's Live view, filtered for the viewer exactly as the tab is.
    204 when nothing is new; the client advances its cursor from X-Live-Cursor."""
    if view_filter not in HOME_LIVE_FILTERS:
        abort(404)
    after = request.args.get('after', type=int)
    if after is None:
        abort(400)

    community_ids, community_sql = home_feed_source(view_filter)
    post_ids = get_deduped_post_ids('', community_ids, 'new', include_following=view_filter == 'subscribed',
                                    community_sql=community_sql,
                                    newer_than=(after, utcnow() - LIVE_WINDOW))[:LIVE_LIMIT]
    if not post_ids:
        return '', 204

    user_id = current_user.get_id()
    response = make_response(render_template(
        'community/_live_posts.html', posts=post_ids_to_models(post_ids, 'new'), sort='new',
        show_post_community=True, low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
        content_filters=user_filters_home(current_user.id),
        recently_upvoted=recently_upvoted_posts(current_user.id),
        recently_downvoted=recently_downvoted_posts(current_user.id),
        communities_banned_from_list=communities_banned_from(current_user.id),
        reported_posts=reported_posts(user_id, user_id in g.admin_ids), user_notes=user_notes(user_id),
        joined_communities=joined_or_modding_communities(user_id),
        moderated_community_ids=moderating_communities_ids(user_id), user_pronouns=user_pronouns()))
    response.headers['X-Live-Cursor'] = str(max(post_ids))
    return response


def home_feed_source(view_filter: str):
    """The communities a home tab draws from, for the current viewer, as get_deduped_post_ids
    takes them: (community_ids, community_sql). Shared by the home page and its Live fragment."""
    # view filter - subscribed/local/all
    community_ids = [-1]
    low_quality_filter = 'AND c.low_quality is false' if current_user.is_authenticated and current_user.hide_low_quality else ''
    if current_user.is_authenticated:
        modded_communities = moderating_communities_ids(current_user.id)
        pc = community_membership_private(current_user.id)
        if len(pc) == 0:    # tuples must have at least 2 elements, I think?
            private_communities = tuple([0, 0])
        else:
            private_communities = tuple(pc + [0])
    else:
        modded_communities = []
        private_communities = ()
    if len(private_communities) == 0:
        private_communities = tuple([0, 0])

    community_sql = None
    if view_filter == 'subscribed' and current_user.is_authenticated:
        community_ids = db.session.execute(text(
            'SELECT id FROM community as c INNER JOIN community_member as cm ON cm.community_id = c.id WHERE cm.is_banned is false AND cm.user_id = :user_id'),
                                           {'user_id': current_user.id}).scalars()
    elif view_filter == 'local':
        microblog_community = find_microblogging_community()
        if current_user.is_anonymous:
            community_sql = f'c.private is false and c.instance_id = 1 and c.id != {microblog_community.id} {low_quality_filter}'
        else:
            community_sql = f'(c.private is false OR c.id IN {private_communities}) and c.id != {microblog_community.id} AND c.instance_id = 1 {low_quality_filter}'
        community_ids = [0]
    elif view_filter == 'popular':
        if current_user.is_anonymous:
            community_sql = 'c.show_popular is true and c.private is false AND c.low_quality is false'
        else:
            community_sql = f'(c.private is false OR c.id IN {private_communities}) AND c.show_popular is true {low_quality_filter}'
        community_ids = [0]
    elif view_filter == 'media':   # D24: PeerTube and Castopod only, a subset of All
        if current_user.is_anonymous:
            community_sql = f'{MEDIA_COMMUNITY_SQL} AND c.show_all is true AND c.private is false AND c.low_quality is false'
        else:
            community_sql = f'(c.private is false OR c.id IN {private_communities}) AND c.show_all is true AND {MEDIA_COMMUNITY_SQL} {low_quality_filter}'
        community_ids = [0]
    elif view_filter == 'all' or current_user.is_anonymous:
        community_ids = [-1]  # Special value to indicate 'All'
    elif view_filter == 'moderating':
        community_ids = modded_communities

    community_ids = list(community_ids)
    return community_ids, community_sql


def home_page(sort, view_filter, page, result_id, low_bandwidth, tag, live=False):

    page_length = 20 if low_bandwidth else current_app.config['PAGE_LENGTH']

    if current_user.is_authenticated and current_user.page_length and current_user.page_length < page_length:
        page_length = current_user.page_length

    if current_user.is_authenticated:
        ensure_rss_token(current_user)  # so a private rss feed can be generated
        modded_communities = moderating_communities_ids(current_user.id)
    else:
        modded_communities = []
    enable_mod_filter = len(modded_communities) > 0
    community_ids, community_sql = home_feed_source(view_filter)

    query_sort = 'new' if live else sort  # Live is the New order, kept current by live_feed.js
    post_ids = get_deduped_post_ids('' if live else result_id, community_ids, query_sort, tag,
                                    include_following=view_filter == 'subscribed' and current_user.is_authenticated,
                                    community_sql=community_sql)
    has_next_page = len(post_ids) > (page + 1) * page_length  # page is 0-based; `page + 1 * page_length` was page + page_length (D781)
    post_ids = paginate_post_ids(post_ids, page, page_length=page_length)
    posts = post_ids_to_models(post_ids, query_sort)
    live_cursor = max(post_ids, default=0) if live else 0

    if page == 0 and not live:
        # First page of the home feed, include any instance-wide stickies if they are present and visible
        instance_stickies = get_instance_stickies(community_ids=community_ids, sort=sort)
    else:
        instance_stickies = []

    if current_user.is_anonymous:
        content_filters = {'-1': {'trump', 'elon', 'musk'}}
    else:
        content_filters = user_filters_home(current_user.id)

    # Pagination
    if live:  # older posts continue in the plain New list
        next_url = url_for('main.index', page=page + 1, sort='new', view_filter=view_filter) if has_next_page else None
    else:
        next_url = url_for('main.index', page=page + 1, sort=sort, view_filter=view_filter,
                           result_id=result_id) if has_next_page else None
    prev_url = url_for('main.index', page=page - 1, sort=sort, view_filter=view_filter,
                       result_id=result_id) if page > 0 else None

    # Sidebar
    active_communities = sidebar_active_communities(current_user.get_id())
    new_communities = sidebar_new_communities(current_user.get_id())
    instances = sidebar_new_instances()
    upcoming_events = sidebar_upcoming_events()

    # Voting history and ban status
    if current_user.is_authenticated:
        recently_upvoted = recently_upvoted_posts(current_user.id)
        recently_downvoted = recently_downvoted_posts(current_user.id)
        communities_banned_from_list = communities_banned_from(current_user.id)
    else:
        recently_upvoted = []
        recently_downvoted = []
        communities_banned_from_list = []
    
    user_id = current_user.get_id()

    rss_token = f'?token={current_user.rss_token}' if current_user.is_authenticated else ''

    if current_user.is_anonymous:
        rss_feed = [
            (_('Local posts'), f'index/feed/local'),
        ]
    else:
        rss_feed = [
            (_('Posts from joined communities'), f'index/feed/subscribed{rss_token}'),
            (_('Local posts'), f'index/feed/local{rss_token}'),
            (_('Posts from popular communities'), f'index/feed/popular{rss_token}'),
            (_('All posts'), f'index/feed/all{rss_token}'),
        ]

    if request.args.get('fragment'):
        resp = make_response(render_template('index_fragment.html', posts=posts,
                                             show_post_community=True, low_bandwidth=low_bandwidth,
                                             recently_upvoted=recently_upvoted,
                                             recently_downvoted=recently_downvoted,
                                             communities_banned_from_list=communities_banned_from_list,
                                             SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING,
                                             SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                                             etag=f"{sort}_{view_filter}_{hash(str(g.site.last_active))}",
                                             instance_stickies=instance_stickies,
                                             content_filters=content_filters, sort=sort, view_filter=view_filter,
                                             reported_posts=reported_posts(user_id, user_id in g.admin_ids),
                                             user_notes=user_notes(user_id),
                                             joined_communities=joined_or_modding_communities(user_id),
                                             moderated_community_ids=moderating_communities_ids(user_id),
                                             enable_mod_filter=enable_mod_filter,
                                             has_topics=num_topics() > 0, time=time,
                                             user_pronouns=user_pronouns(),
                                             ))
    else:
        resp = make_response(render_template('index.html', posts=posts, active_communities=active_communities,
                               new_communities=new_communities, upcoming_events=upcoming_events,
                               show_post_community=True, low_bandwidth=low_bandwidth, recently_upvoted=recently_upvoted,
                               recently_downvoted=recently_downvoted, new_instances=instances,
                               communities_banned_from_list=communities_banned_from_list,
                               SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                               etag=f"{sort}_{view_filter}_{hash(str(g.site.last_active))}", next_url=next_url,
                               prev_url=prev_url, instance_stickies=instance_stickies,
                               title=f"{g.site.name} - {g.site.description}",
                               description=shorten_string(html_to_text(g.site.sidebar), 150),
                               content_filters=content_filters, sort=sort, view_filter=view_filter,
                               announcement=get_setting('announcement_html', get_setting('announcement')),
                               reported_posts=reported_posts(user_id, user_id in g.admin_ids),
                               user_notes=user_notes(user_id),
                               joined_communities=joined_or_modding_communities(user_id),
                               moderated_community_ids=moderating_communities_ids(user_id),
                               inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                               enable_mod_filter=enable_mod_filter,
                               has_topics=num_topics() > 0, time=time,
                               user_pronouns=user_pronouns(),
                               rss_feed=rss_feed, live=live,
                               live_cursor=live_cursor, live_filters=HOME_LIVE_FILTERS,
                               live_key=home_live_key(view_filter) if live else '',
                               reload_url='' if live else reload_url(sort, view_filter)
                               ))
    if current_user.is_anonymous:
        resp.headers.set('ETag', f"{sort}_{view_filter}_{hash(str(g.site.last_active))}")
        resp.headers.set('Vary', 'Accept, Accept-Language')
        resp.headers.set('Cache-Control', 'public, max-age=60')
    else:
        resp.headers.set('Vary', 'Accept, Cookie, Accept-Language')
        resp.headers.set('Cache-Control', 'private, max-age=15, must-revalidate')

    return resp


@bp.route('/topics', methods=['GET'])
@login_required_if_private_instance
def list_topics():
    verification_warning()
    topics = topic_tree()

    return render_template('list_topics.html', topics=topics, title=_('Browse by topic'),
                           low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1')


@bp.route('/add_post', methods=['GET'])
@login_required
def add_post():
    """The "add post" button: pick a community for the user and send them to its
    compose form.

    D1385. `cross_post_community_id` is a cookie, written when the user last
    cross-posted, and it was trusted three ways at once. Measured, every value
    below reaching this route:

        'abc', 'null', '1.5'   ValueError: invalid literal for int() with base 10
        '0', '2', '999999'     AttributeError: 'NoneType' object has no attribute
                               'link'   (no such community)
        '-1'                   204, with the joined-community fallback skipped

    So a cookie naming a community that has since been deleted -- or one edited by
    hand, or left by an older version -- broke the button with a 500 until it
    expired, and a `-1` made it silently do nothing. The cookie is a hint about
    where the user probably wants to post, so an unusable one falls through to the
    same choice the route would have made without it, and 204 is left for the case
    it means: this user has nowhere to post.
    """
    poss_communities = possible_communities()

    default_community = None
    cross_post_community_id = request.cookies.get('cross_post_community_id')
    if cross_post_community_id and cross_post_community_id.strip().isdigit():
        default_community = db.session.get(Community,
                                           int(cross_post_community_id))

    if default_community is None:
        for section in ("Joined communities", "Moderating", "Others"):
            if section in poss_communities and poss_communities[section]:
                default_community = db.session.get(
                    Community, poss_communities[section][0][0])
                if default_community is not None:
                    break

    if default_community is None:
        return ('', 204)
    return redirect(url_for('community.add_post', actor=default_community.link()))


@bp.route('/communities', methods=['GET'])
@limiter.limit("20 per 1 minutes", methods=['GET', 'POST'])
@login_required_if_private_instance
@check_anoobis
def list_communities():
    verification_warning()
    search_param = request.args.get('search', '').strip()
    home_select = request.args.get('home_select', 'any')
    subscribe_select = request.args.get('subscribe_select', 'any')
    # D1311. `int(request.args.get('topic_id', 0))` is a 500 for anything that
    # is not a number, and one of those is the empty string a select sends when
    # nothing is chosen -- so `/communities?topic_id=` crashed a page anybody
    # can reach. `type=int` answers the default instead of raising.
    topic_id = request.args.get('topic_id', 0, type=int)
    feed_id = request.args.get('feed_id', 0, type=int)
    language_id = request.args.get('language_id', 0, type=int)
    nsfw = request.args.get('nsfw', 'all')
    page = request.args.get('page', 1, type=int)
    instance = request.args.get('instance', '')
    platform = request.args.get('platform', '')
    if platform not in MEDIA_SOFTWARE:   # D24: anything else filters nothing
        platform = ''
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    sort_by = request.args.get('sort_by', 'post_reply_count desc')

    if not g.site.enable_nsfw:
        nsfw = 'no'
        hide_nsfw = True
    else:
        hide_nsfw = False

    if request.args.get('prompt'):
        flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))

    topics = topics_for_form(0)
    languages = Language.query.order_by(Language.name).all()
    communities = Community.query.filter_by(banned=False)

    # filter private communities: show only to members
    if current_user.is_authenticated:
        # for authenticated users, show non-private communities OR private communities where they are members
        # `.subquery()` inside `in_()` is an SAWarning ("Coercing Subquery
        # object into a select()") on every request that reaches this line;
        # `.scalar_subquery()` is what SQLAlchemy 2 wants for a one-column
        # subquery and emits the same SQL.
        member_check = db.session.query(CommunityMember.community_id).filter(
            CommunityMember.user_id == current_user.id,
            CommunityMember.is_banned == False
        ).scalar_subquery()
        communities = communities.filter(
            or_(
                Community.private == False,
                Community.id.in_(member_check)
            )
        )
    else:
        # For anonymous users, only show non-private communities
        communities = communities.filter_by(private=False)

    if search_param == '':
        pass
    else:
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id > 0:
        communities = communities.filter_by(topic_id=topic_id)
    elif topic_id < 0:
        communities = communities.filter_by(topic_id=None)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)
    
    if home_select == "local":
        communities = communities.filter(Community.ap_id == None)
    elif home_select == "remote":
        communities = communities.filter(Community.ap_id != None)
    
    if subscribe_select != "any":
        # get the user's joined communities
        user_joined_communities = joined_communities(current_user.get_id())
        user_moderating_communities = moderating_communities(current_user.get_id())
        # get the joined community ids list
        joined_ids = []
        for jc in user_joined_communities:
            joined_ids.append(jc.id)
        for mc in user_moderating_communities:
            joined_ids.append(mc.id)
        
        if subscribe_select == "subscribed":
            # filter down to just the joined communities
            communities = communities.filter(Community.id.in_(joined_ids))
        elif subscribe_select == "not_subscribed":
            # filter out the joined communities from all communities
            communities = communities.filter(Community.id.not_in(joined_ids))

    # default to no public feeds
    server_has_feeds = False
    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all()
    if len(public_feeds) > 0:
        server_has_feeds = True

    create_admin_only = g.site.community_creation_admin_only

    is_admin = current_user.is_authenticated and current_user.is_admin()

    # if filtering by public feed 
    # get all the ids of the communities
    # then filter the communites to ones whose ids match the feed
    if feed_id != 0:
        # D1394. Nothing here asked whether the caller may see this feed. The
        # dropdown the parameter comes from offers public feeds only, but the
        # parameter is a query string: `/communities?feed_id=<a private feed>`
        # answered 200 with exactly the communities inside it, to anybody,
        # while `/f/<that feed's name>` redirected the same visitor away with
        # 'Could not find that feed or it is not public'.
        #
        # A feed the caller may not read is treated as one that names nothing,
        # which is the answer this route already gives for an id that names
        # nothing -- an empty list rather than a 404, so an unreadable feed and
        # an absent one cannot be told apart.
        feed_community_ids = []
        if feed_readable_by(db.session.get(Feed, feed_id), current_user.id if current_user.is_authenticated else None):
            for item in FeedItem.query.filter_by(feed_id=feed_id).all():
                feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))
    
    # if filtering by home instance
    if instance:
        communities = communities.filter(Community.ap_domain == instance)
    if platform:
        communities = communities.filter(platform_community_clause(platform))

    # D1418. `hide_nsfw = False` stood here, throwing away the decision made at the top of
    # this function: an instance with `enable_nsfw` off sets it True, and this line put it
    # straight back to False. The template shows the NSFW All/Yes/No selector when
    # `hide_nsfw` is falsy, so a site that has disabled NSFW still offered the control.
    # Picking `Yes` on it did nothing -- `nsfw` is already forced to 'no' above -- so the
    # visible effect was an inert filter, not communities that should have been hidden.
    # Both branches above assign the name, so nothing here is left unbound.

    if current_user.is_authenticated:
        if current_user.hide_low_quality:
            communities = communities.filter(Community.low_quality == False)
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            nsfw = 'no'
            hide_nsfw = True
            communities = communities.filter(Community.nsfw == False)
        else:
            if nsfw == 'no':
                communities = communities.filter(Community.nsfw == False)
            elif nsfw == 'yes':
                communities = communities.filter(Community.nsfw == True)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
        if blocked_community_ids := blocked_communities(current_user.id):
            communities = communities.filter(Community.id.not_in(blocked_community_ids))
        instance_ids = blocked_or_banned_instances(current_user.id)
        if instance_ids:
            communities = communities.filter(or_(Community.instance_id.not_in(instance_ids), Community.instance_id == None))
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))

    else:
        communities = communities.filter(Community.nsfl == False)
        if nsfw == 'no':
            communities = communities.filter(and_(Community.nsfw == False))
        elif nsfw == 'yes':
            communities = communities.filter(and_(Community.nsfw == True))

    communities = communities.order_by(safe_order_by(sort_by, Community, {'title', 'subscriptions_count', 'post_count',
                                                                          'post_reply_count', 'last_active', 'created_at',
                                                                          'active_weekly'}))

    # dict used for pagination query parameters
    args_dict = dict()
    args_dict["search"] = search_param
    args_dict["home_select"] = home_select
    args_dict["subscribe_select"] = subscribe_select
    args_dict["topic_id"] = topic_id
    args_dict["feed_id"] = feed_id
    args_dict["language_id"] = language_id
    args_dict["nsfw"] = nsfw
    args_dict["instance"] = instance
    args_dict["platform"] = platform

    # Pagination (platform_of reads each row's instance, so load it with the list)
    communities = communities.options(joinedload(Community.instance)).paginate(page=page,
                                       per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
    # Interop D24: below the local results, offer what the discovery directory knows and this server does not
    discovered = discovery_fallback(KIND_COMMUNITY, search_param, allow_nsfw=nsfw != 'no',
                                    viewer_id=current_user.id if current_user.is_authenticated else None) \
        if search_param and page == 1 else []
    context = _base_list_communities_context()
    context["next_url"] = url_for('main.list_communities', page=communities.next_num, sort_by=sort_by,
                       **args_dict) if communities.has_next else None
    context["prev_url"] = url_for('main.list_communities', page=communities.prev_num, sort_by=sort_by, 
                       **args_dict) if communities.has_prev and page != 1 else None

    context.update({
        "communities": communities,
        "search": search_param,
        "title": _('Communities'), 
        "intance": instance,
        "platform": platform,
        "home_select": home_select,
        "topics": topics,
        "languages": languages,
        "topic_id": topic_id,
        "language_id": language_id,
        "sort_by": sort_by,
        "nsfw": nsfw,
        "subscribe_select": subscribe_select,
        "low_bandwidth": low_bandwidth,
        "feed_id": feed_id,
        "server_has_feeds": server_has_feeds,
        "public_feeds": public_feeds,
        "hide_nsfw": hide_nsfw,
        "create_admin_only": create_admin_only,
        "is_admin": is_admin,
        "discovered": discovered,
    })

    return render_template('list_communities.html', **context)


@bp.route('/modlog', methods=['GET'])
@limiter.limit("20 per 1 minutes", methods=['GET', 'POST'])
# A private instance shows nothing to a caller without an account, and this
# page was the exception: it answered 200 with its public entries -- community
# names, actions and reasons -- where /communities and / redirect to the login.
# The `public == True` filter below still governs what a signed-in
# non-moderator sees on a public instance.
@login_required_if_private_instance
@check_anoobis
def modlog():
    page = request.args.get('page', 1, type=int)
    low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
    mod_action = request.args.get('mod_action', '')
    suspect_user_name = request.args.get('suspect_user_name', '')
    # D1395, and D1389's shape verbatim: `int()` behind a `!= ''` test, which
    # only rules out the empty string. `?communities=abc` was
    # `ValueError: invalid literal for int() with base 10: 'abc'` and a 500 on a
    # page anybody can open -- the modlog is public. `type=int` answers the
    # default instead of raising, as :313 records for this same file.
    community_id = request.args.get('communities', 0, type=int)
    user_name = request.args.get('user_name', '')
    can_see_names = False
    is_admin = False

    arg_dict = {"low_bandwidth": low_bandwidth,
                "mod_action": mod_action,
                "suspect_user_name": suspect_user_name,
                "communities": community_id,
                "user_name": user_name}

    # Admins can see all of the modlog, everyone else can only see public entries
    modlog_entries = ModLog.query
    if mod_action:
        modlog_entries = modlog_entries.filter(ModLog.action == mod_action)
    if suspect_user_name:
        if f"@{current_app.config['SERVER_NAME']}" in suspect_user_name:
            suspect_user_name = suspect_user_name.split('@')[0]
        user = User.query.filter(func.lower(User.user_name) == suspect_user_name.lower(), User.ap_id == None).first()
        if user is None:
            user = User.query.filter_by(ap_id=suspect_user_name.lower()).first()
        if user:
            modlog_entries = modlog_entries.filter(ModLog.target_user_id == user.id)
            if not (current_user.is_authenticated and (current_user.is_admin() or current_user.is_staff())):
                # R3: an entry about content that is not open names no target user to this reader
                modlog_entries = modlog_entries.filter(modlog_open_clause())
    if user_name:
        if f"@{current_app.config['SERVER_NAME']}" in user_name:
            user_name = user_name.split('@')[0]
        # `user_name`, not `suspect_user_name`: this block filters by the
        # MODERATOR who acted, and it was searching for the suspect's name
        # instead -- a copy of the block above with one word left behind. With
        # no suspect named, `''.lower()` matched nobody, `user` stayed None,
        # and the fallback lookup by ap_id matches only remote accounts -- so
        # filtering the modlog by a local moderator's name silently returned
        # the whole unfiltered log.
        user = User.query.filter(func.lower(User.user_name) == user_name.lower(), User.ap_id == None).first()
        if user is None:
            user = User.query.filter_by(ap_id=user_name.lower()).first()
        if user:
            modlog_entries = modlog_entries.filter(ModLog.user_id == user.id)
    if community_id:
        modlog_entries = modlog_entries.filter(ModLog.community_id == community_id)

    if current_user.is_authenticated:
        if current_user.is_admin() or current_user.is_staff():
            is_admin = True
            modlog_entries = modlog_entries.order_by(desc(ModLog.created_at))
            can_see_names = True
        else:
            modlog_entries = modlog_entries.filter(ModLog.public == True).order_by(desc(ModLog.created_at))
    else:
        modlog_entries = modlog_entries.filter(ModLog.public == True).order_by(desc(ModLog.created_at))

    # Pagination
    modlog_entries = modlog_entries.paginate(page=page, per_page=100 if not low_bandwidth else 50, error_out=False)
    next_url = url_for('main.modlog', page=modlog_entries.next_num, **arg_dict) if modlog_entries.has_next else None
    prev_url = url_for('main.modlog', page=modlog_entries.prev_num, **arg_dict) if modlog_entries.has_prev and page != 1 else None

    instances = {instance.id: instance.domain for instance in Instance.query.all()}
    communities = {community.id: community.display_name() for community in Community.query.filter(Community.banned == False).all()}
    community_trusted = db.session.execute(text('SELECT c.id FROM "community" as c INNER JOIN "instance" as i on c.instance_id = i.id WHERE i.trusted is true or i.id = 1')).scalars()

    return render_template('modlog.html',
                           title=_('Moderation Log'), modlog_entries=modlog_entries, can_see_names=can_see_names,
                           next_url=next_url, prev_url=prev_url, low_bandwidth=low_bandwidth,
                           instances=instances, is_admin=is_admin, communities=communities,
                           mod_action=mod_action, suspect_user_name=suspect_user_name, community_id=community_id,
                           user_name=user_name, community_trusted=list(community_trusted),
                           inoculation=inoculation[randint(0, len(inoculation) - 1)] if g.site.show_inoculation_block else None,
                           )


@bp.route("/modlog/search_suggestions", methods=['POST'])
# D1375. The page this serves is `@login_required_if_private_instance` -- the comment
# above `modlog()` records why: the modlog was the one page that answered 200 to an
# anonymous visitor where `/communities` and `/` redirect to the login. Its typeahead
# endpoint had no gate at all, so on a private instance `/modlog` answered 302 while
# this answered 200 and named users, five accounts per substring, to anybody. One
# control, a page and the endpoint it feeds, applied to the page.
@login_required_if_private_instance
def modlog_search_suggestions():
    q = request.form.get("suspect_user_name", "").lower()
    if q == '':
        q = request.form.get("user_name", "").lower()
    results = User.query.filter(or_(User.ap_id.ilike(f"%{q}%"),
                                    User.user_name.ilike(f"%{q}%"),
                                    User.ap_profile_id.ilike(f"%{q}%"))
                                ).limit(5).all()
    # D1375. `f"<option value='{m.ap_id or m.user_name}'>"`. A user name is not a safe
    # HTML attribute value: nothing restricts the characters in a REMOTE actor's name
    # -- `actor_name_from_ap` strips it and cuts it to the column and does not filter
    # it -- so a peer publishing `preferredUsername: "x'><img src=x onerror=alert(1)>"`
    # had that stored verbatim, and this endpoint echoed it into a response
    # modlog.html swaps into the DOM with htmx. Measured: the body came back as
    # `<option value='x'><img src=x onerror=alert(1)>'>`, the tag having left the
    # attribute. Jinja autoescapes; this string never went through Jinja.
    return "".join(f"<option value='{escape(m.ap_id or m.user_name)}'>"
                   for m in results)


@bp.route('/about')
def about_page():
    user_amount = users_total()
    MAU = active_month()
    posts_amount = local_posts()

    admins = Site.admins()
    staff = Site.staff()
    domains_amount = db.session.execute(text('SELECT COUNT(*) as c FROM "domain" WHERE "banned" IS false')).scalar()
    community_amount = local_communities()
    instance = Instance.query.filter_by(id=1).first()

    cms_page = CmsPage.query.filter(CmsPage.url == '/about').first()

    return render_template('about.html', user_amount=user_amount, mau=MAU, posts_amount=posts_amount,
                           domains_amount=domains_amount, community_amount=community_amount, instance=instance,
                           admins=admins, staff=staff, cms_page=cms_page)


@bp.route('/privacy')
def privacy():
    cms_page = CmsPage.query.filter(CmsPage.url == '/privacy').first()
    if cms_page:
        return render_template('cms_page.html', page=cms_page)
    return render_template('privacy.html')


@bp.route('/login')
def login():
    return redirect(url_for('auth.login'))


@bp.route('/robots.txt')
def robots():
    resp = make_response(render_template('robots.txt', use_rsl=not current_app.config['ALLOW_AI_CRAWLERS']))
    resp.mimetype = 'text/plain'
    return resp


@bp.route('/.well-known/security.txt')
def security():
    resp = make_response(render_template('security.txt'))
    resp.mimetype = 'text/plain'
    return resp


@bp.route('/sitemap.xml')
@refuse_if_private_instance
@cache.cached(timeout=6000)
def sitemap():
    posts = Post.query.join(Community, Post.community_id == Community.id).filter(
        Post.from_bot == False, Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
        Post.instance_id == 1, Post.indexable == True, Community.private == False, Community.local_only == False,
        listable_clause(Post))
    posts = posts.order_by(desc(Post.posted_at)).limit(500)

    resp = make_response(render_template('sitemap.xml', posts=posts, current_app=current_app))
    resp.mimetype = 'text/xml'
    return resp


@bp.route('/rsl.xml')
def rsl():
    if current_app.config['ALLOW_AI_CRAWLERS']:
        abort(404)
    resp = make_response(render_template('rsl.xml'))
    resp.mimetype = 'text/xml'
    return resp


@bp.route('/keyboard_shortcuts')
def keyboard_shortcuts():
    return render_template('keyboard_shortcuts.html')


@bp.route('/replay_inbox')
@login_required
def replay_inbox():

    request_json = {}
    """
    request_json = {"@context": ["https://join-lemmy.org/context.json", "https://www.w3.org/ns/activitystreams"],
                    "actor": "https://lemmy.lemmy/u/doesnotexist",
                    "cc": [],
                    "id": "https://lemmy.lemmy/activities/delete/5d42c8bf-cc60-4d2c-a3b5-673ddb7ce64b",
                    "object": "https://lemmy.lemmy/u/doesnotexist",
                    "to": ["https://www.w3.org/ns/activitystreams#Public"],
                    "type": "Delete"}
    """

    replay_inbox_request(request_json)

    return 'ok'


@bp.route('/honey')
@bp.route('/honey/<whatever>')
def honey_pot(whatever=None):
    if current_user.is_authenticated:
        return ''
    else:
        do_not_track = ['image', 'audio', 'video']
        if request.headers.get('Sec-Fetch-Dest', '') in do_not_track or request.headers.get('Accept', '').startswith('image/'):
            return ''
    ip = ip_address()
    key = f"honeypot:{ip}"

    now = time.time()
    score = now
    member = str(now)  # unique enough for repeated entries

    added = app_pkg.redis_client.zadd(key, {member: score})

    if added == 1 and app_pkg.redis_client.ttl(key) == -1:
        app_pkg.redis_client.expire(key, 86400)  # auto-expire key after 24h of inactivity

    # Remove entries older than 24 hours
    app_pkg.redis_client.zremrangebyscore(key, 0, now - 86400)

    # Count recent events
    count = app_pkg.redis_client.zcount(key, now - 86400, now)

    if count >= 3:
        app_pkg.redis_client.set(f"ban:{ip}", 1, ex=86400 * 7 * 4)  # ban scraper for 4 weeks

    return gibberish(100)


@bp.route('/test')
@debug_mode_only
def test():
    #refresh_instance_chooser()
    #p = Post.query.get(42)
    #p.delete_dependencies()
    #db.session.delete(p)
    #db.session.commit()
    return markdown_to_html('Testing!\n\n![an image :: width=50](https://piefed.social/static/media/logo_8p7en.svg, https://media.piefed.social/posts/up/TR/upTRjfvFt2ma0hz.webp)\n\nthere we go')

    community = db.session.get(Community, 33)
    announce_activity = {
        'actor': community.ap_profile_id,
        'id': f'xyz{gibberish()}',
        'object': {
            'actor': 'https://piefed.rimu.geek.nz/u/rimu',
            'id': f'xyz2{gibberish()}',
            'object': 'https://piefed.ngrok.app/u/rimu',
            'type': 'Like',
        },
        'type': 'Announce',
    }

    send_async = []
    send_async.append(HttpSignature.signed_request('https://piefed.ngrok.app/inbox', announce_activity,
                                                   community.private_key,
                                                   community.profile_id() + '#main-key',
                                                   send_via_async=True))

    # send announce_activity via redis pub/sub to piefed_notifs service
    app_pkg.redis_client.publish("http_posts:activity", json.dumps({'urls': [url[0] for url in send_async],
                                                            'headers': [url[1] for url in send_async],
                                                            'data': send_async[0][2].decode('utf-8')}))

    return 'Done'
    user_id = 1
    r = get_redis_connection()
    r.publish(f"notifications:{user_id}", json.dumps({'num_notifs': randint(1, 100)}))
    current_user.unread_notifications = randint(1, 100)
    db.session.commit()
    return 'Done'

    user = db.session.get(User, 1)
    send_registration_approved_email(user)

    markdown = """What light novels have you read in the past week? Something good? Bad? Let us know about it. 

And if you want to add your score to the database to help your fellow Bookworms find new reading materials you can use the following template:

><Book Title and Volume> Review Goes Here [5/10]"""

    return markdown_to_html(markdown)

    return ip_address()

    json = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "actor": "https://ioc.exchange/users/haiviittech",
        "id": "https://ioc.exchange/users/haiviittech#delete",
        "object": "https://ioc.exchange/users/haiviittech",
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "type": "Delete"
    }

    r = db.session.get(User, 1)

    jsonld.set_document_loader(jsonld.requests_document_loader(timeout=5))

    ld = LDSignature.create_signature(json, r.private_key, r.public_url() + '#main-key')
    json.update(ld)

    LDSignature.verify_signature(json, r.public_key)

    # for community in Community.query.filter(Community.content_retention != -1):
    #    for post in community.posts.filter(Post.posted_at < utcnow() - timedelta(days=Community.content_retention)):
    #        post.delete_dependencies()

    return 'done'

    md = "::: spoiler I'm all for ya having fun and your right to hurt yourself.\n\nI am a former racer, commuter, and professional Buyer for a chain of bike shops. I'm also disabled from the crash involving the 6th and 7th cars that have hit me in the last 170k+ miles of riding. I only barely survived what I simplify as a \"broken neck and back.\" Cars making U-turns are what will get you if you ride long enough, \n\nespecially commuting. It will look like just another person turning in front of you, you'll compensate like usual, and before your brain can even register what is really happening, what was your normal escape route will close and you're going to crash really hard. It is the only kind of crash that your intuition is useless against.\n:::"

    return markdown_to_html(md)

    users_to_notify = User.query.join(Notification, User.id == Notification.user_id).filter(
        User.ap_id == None,
        Notification.created_at > User.last_seen,
        Notification.read == False,
        User.email_unread_sent == False,  # they have not been emailed since last activity
        User.email_unread == True  # they want to be emailed
    ).all()

    for user in users_to_notify:
        notifications = Notification.query.filter(Notification.user_id == user.id, Notification.read == False,
                                                  Notification.created_at > user.last_seen).all()
        if notifications:
            # Also get the top 20 posts since their last login
            posts = Post.query.join(CommunityMember, Post.community_id == CommunityMember.community_id).filter(
                CommunityMember.is_banned == False, listable_clause(Post))
            posts = posts.filter(CommunityMember.user_id == user.id)
            if user.ignore_bots == 1:
                posts = posts.filter(Post.from_bot == False)
            if user.hide_nsfl == 1:
                posts = posts.filter(Post.nsfl == False)
            if user.hide_nsfw == 1:
                posts = posts.filter(Post.nsfw == False)
            domains_ids = blocked_domains(user.id)
            if domains_ids:
                posts = posts.filter(or_(Post.domain_id.not_in(domains_ids), Post.domain_id == None))
            posts = posts.filter(Post.posted_at > user.last_seen).order_by(desc(Post.score))
            posts = posts.limit(20).all()

            # Send email!
            send_email(_('[PieFed] You have unread notifications'),
                       sender=f'{g.site.name} <{current_app.config["MAIL_FROM"]}>',
                       recipients=[user.email],
                       text_body=flask.render_template('email/unread_notifications.txt', user=user,
                                                       notifications=notifications),
                       html_body=flask.render_template('email/unread_notifications.html', user=user,
                                                       notifications=notifications,
                                                       posts=posts,
                                                       domain=current_app.config['SERVER_NAME']))
            user.email_unread_sent = True
            db.session.commit()

    return 'ok'


@bp.route('/communities_menu')
def communities_menu():
    if current_user.is_authenticated:
        favorites = Community.query.filter(Community.id.in_(favorite_communities(current_user.id))).all()
    else:
        favorites = None
    return render_template('communities_menu.html',
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()),
                           is_admin=current_user.is_authenticated and current_user.is_admin(),
                           is_staff=current_user.is_authenticated and current_user.is_staff(),
                           default_user_add_remote=get_setting("allow_default_user_add_remote_community", True),
                           favorite_communities=favorites
                           )


@bp.route('/explore_menu')
def explore_menu():
    return render_template('explore_menu.html', menu_topics=menu_topics(),
                           menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None
                           )


@bp.route('/topics_menu')
def topics_menu():
    return render_template('topics_menu.html', menu_topics=menu_topics(),
                           moderating_communities=moderating_communities(current_user.get_id()),
                           joined_communities=joined_communities(current_user.get_id()))


@bp.route('/feeds_menu')
def feeds_menu():
    return render_template('feeds_menu.html',
                           menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None,
                           )


@bp.route('/share', methods=['GET', 'POST'])
def share():
    # `/share` is a public GET that anything can follow, and a request with no
    # `url` at all used to be `AttributeError: 'NoneType' object has no
    # attribute 'strip'` -- a 500, a logged traceback and a Sentry event, for a
    # request that is merely incomplete.
    url = request.args.get('url')
    if not url:
        abort(400)
    url = remove_tracking_from_link(url.strip())
    form = ShareLinkForm()
    form.which_community.choices = possible_communities()
    if form.validate_on_submit():
        community = db.session.get(Community, form.which_community.data) or abort(404)
        response = make_response(redirect(url_for('community.add_post', actor=community.link(), type='link', link=url,
                                                  title=request.args.get('title'))))
        response.set_cookie('cross_post_community_id', str(community.id), max_age=timedelta(days=28))
        response.delete_cookie('post_title')
        response.delete_cookie('post_description')
        response.delete_cookie('post_tags')
        return response

    # D1386. `int(request.cookies.get(...))` -- the third reader of this cookie,
    # and the last unguarded one. `/share` is a PUBLIC route that anything can
    # follow, so a non-numeric cookie was `ValueError: invalid literal for int()
    # with base 10: 'abc'` and a 500, measured. The other two readers already
    # guard it: app/post/routes.py:2680 catches `(TypeError, ValueError)` and
    # checks the row resolves -- its comment names both failures -- and
    # `main.add_post` above was fixed as D1385. This is the same shape at the one
    # site neither round reached.
    #
    # Pre-selecting the last community is a convenience, so an unusable cookie
    # leaves the field empty rather than breaking the page.
    remembered = request.cookies.get('cross_post_community_id')
    if remembered and remembered.strip().isdigit():
        last_community = db.session.get(Community, int(remembered))
        if last_community is not None:
            form.which_community.data = last_community.id

    # D1387. These two queries feed `share.html`, which does
    # `posts_keyed_by_community[community.id]` -- so every community the first one
    # lists must have a post in the second one's dict. They did not agree: the
    # post query excludes `Post.microblog == False` and this one only excluded the
    # community NAMED 'microblogs'. A microblog post -- what a Mastodon Note with
    # no title becomes, in any community -- put its community in the list with no
    # entry in the dict, and the template raised
    # `UndefinedError: dict object has no element 1`. Measured: a 500 on a PUBLIC
    # route, for anyone sharing a link some microblog post already carries.
    communities = Community.query.filter_by(banned=False).join(Post).filter(Post.url == url, Post.deleted == False,
                                                                            Post.status > POST_STATUS_REVIEWING,
                                                                            Post.microblog == False,
                                                                            Post.from_bot == False,
                                                                            Community.name != 'microblogs',
                                                                            visible_to_clause(Post, None)).all()
    posts_keyed_by_community = {}
    if len(communities):
        posts = Post.query.filter(Post.url == url, Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                                  Post.microblog == False, Post.from_bot == False, visible_to_clause(Post, None)).all()
        for post in posts:
            posts_keyed_by_community[post.community_id] = post

    return render_template('share.html', form=form, title=request.args.get('title'), communities=communities,
                           posts_keyed_by_community=posts_keyed_by_community)


@bp.route('/protocol_handler')
@login_required
def protocol_handler():
    """ handles the web+fedi:// protocol and redirects the viewer to the right place """
    q = request.args.get('to')
    if q:
        try:
            resp = get_resolve_object(None, {'q': q.replace('web+ap://', 'https://')}, user_id=current_user.id)
        except Exception:
            # `url=q`: the placeholder had no value to substitute, so the
            # visitor was shown the literal '%(url)s'.
            flash(_('Failed to look up %(url)s', url=q))
            return redirect(url_for('main.index'))

        if 'post' in resp:
            post = db.session.get(Post, resp['post']['post']['id'])
            return redirect(post.slug if post.slug else url_for('activitypub.post_ap', post_id=post.id))
        if 'comment' in resp:
            return redirect(url_for('activitypub.comment_ap', comment_id=resp['comment']['comment']['id']))
        if 'community' in resp:
            return redirect(url_for('activitypub.community_profile', actor=resp['community']['community']['id']))
        if 'person' in resp:
            return redirect(url_for('activitypub.user_profile', actor=resp['person']['person']['id']))

        # Anything else the resolver answers -- a feed, say, or nothing at all
        # -- used to fall off the end of the view, and a view that returns None
        # is a 500 rather than "could not find that".
        flash(_('Failed to look up %(url)s', url=q))
        return redirect(url_for('main.index'))
    else:
        return render_template('protocol_handler.html', title=_('Protocol handler'))


@bp.route('/test_email')
@debug_mode_only
def test_email():
    if current_user.is_anonymous:
        email = request.args.get('email')
    else:
        email = current_user.email
    send_email(subject='This is a test email',
               sender=f'{g.site.name} <{current_app.config["MAIL_FROM"]}>',
               recipients=[email],
               text_body='This is a test email. If you received this, email sending is working!',
               html_body='<p>This is a test email. If you received this, email sending is working!</p>',
               reply_to=g.site.contact_email)
    return f'Email sent to {email}.'


@bp.route('/test_redis')
@debug_mode_only
def test_redis():
    if app_pkg.redis_client and app_pkg.redis_client.memory_stats():
        return 'Redis connection is ok'
    else:
        return 'Redis error'


@bp.route('/test_ip')
@debug_mode_only
def test_ip():
    return ip_address() + ' ' + request.headers.get('CF-Connecting-IP', 'CF-Connecting-IP is empty')


@bp.route('/test_s3')
@debug_mode_only
def test_s3():
    boto3_session = boto3.session.Session()
    s3 = boto3_session.client(
        service_name='s3',
        region_name=current_app.config['S3_REGION'],
        endpoint_url=current_app.config['S3_ENDPOINT'],
        aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
        aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
    )
    s3.upload_file('babel.cfg', current_app.config['S3_BUCKET'], 'babel.cfg')
    s3.delete_object(Bucket=current_app.config['S3_BUCKET'], Key='babel.cfg')
    return 'Ok'


@bp.route('/test_hashing')
@debug_mode_only
def test_hashing():
    hash = retrieve_image_hash(f'{current_app.config["SERVER_URL"]}/static/images/apple-touch-icon.png')
    if hash:
        return 'Ok'
    else:
        return 'Error'


@bp.route('/test_ldap')
@debug_mode_only
def test_ldap():
    try:
        # Test LDAP connection
        connection_result = test_ldap_connection()
        if not connection_result:
            return 'LDAP test failed: Could not connect to LDAP server. Check configuration.'

        # Test user sync with dummy data using random password
        random_password = f'testpass{randint(1000, 9999)}'
        sync_result = sync_user_to_ldap('testuser', 'test@example.com', random_password)

        return f'LDAP test successful. Connection: {connection_result}, Sync: {sync_result}'
    except Exception as e:
        return f'LDAP test failed: {str(e)}'


@bp.route('/test_ldap_login')
@debug_mode_only
def test_ldap_login():
    try:
        # Test user login with given user name and password
        login_result = login_with_ldap(request.args.get('user_name'), request.args.get('password'))

        return f'LDAP test results: {str(login_result is not False)}'
    except Exception as e:
        return f'LDAP test failed: {str(e)}'


@bp.route('/test_libretranslate')
@debug_mode_only
def test_libretranslate():
    lt = LibreTranslateAPI(current_app.config['TRANSLATE_ENDPOINT'], api_key=current_app.config['TRANSLATE_KEY'])
    return lt.translate('<p>Si vous lisez cela en anglais, alors la traduction a fonctionné!</p>', source='auto', target='en')


@bp.route('/find_voters')
@login_required
@permission_required('change instance settings')
def find_voters():
    user_ids = db.session.execute(text('SELECT id from "user" ORDER BY last_seen DESC LIMIT 5000')).scalars()
    voters = {}
    for user_id in user_ids:
        recently_downvoted = recently_downvoted_posts(user_id)
        if len(recently_downvoted) > 10:
            voters[user_id] = str(recently_downvoted)

    return str(find_duplicate_values(voters))


def find_duplicate_values(dictionary):
    # Create a dictionary to store the keys for each value
    value_to_keys = {}

    # Iterate through the input dictionary
    for key, value in dictionary.items():
        # If the value is not already in the dictionary, add it
        if value not in value_to_keys:
            value_to_keys[value] = [key]
        else:
            # If the value is already in the dictionary, append the key to the list
            value_to_keys[value].append(key)

    # Filter out the values that have only one key (i.e., unique values)
    duplicates = {value: keys for value, keys in value_to_keys.items() if len(keys) > 1}

    return duplicates


def verification_warning():
    if hasattr(current_user, 'verified') and not current_user.verified:
        flash(_('Please click the link in your email inbox to verify your account.'), 'warning')


@cache.cached(timeout=6)
def activitypub_application():
    application_data = {
        '@context': default_context(),
        'type': 'Application',
        'id': f"{current_app.config['SERVER_URL']}/",
        'name': 'PieFed',
        # Both halves are nullable, and `Site()` with no arguments -- which is
        # what app/models.py and app/admin/routes.py fall back to when row 1 is
        # missing -- leaves them so. `None + ' - '` is a TypeError, and this is
        # the document every fediverse peer fetches when it first hears of this
        # instance, so a site whose tagline was never filled in answered its
        # introductions with a 500.
        'summary': ' - '.join(part for part in (g.site.name, g.site.description) if part),
        'published': ap_datetime(g.site.created_at),
        'updated': ap_datetime(g.site.updated),
        'inbox': f"{current_app.config['SERVER_URL']}/inbox",
        'outbox': f"{current_app.config['SERVER_URL']}/site_outbox",
        'icon': {
            'type': 'Image',
            'url': f"{current_app.config['SERVER_URL']}/static/images/piefed_logo_icon_t_75.png"
        },
        'publicKey': {
            'id': f"{current_app.config['SERVER_URL']}/#main-key",
            'owner': f"{current_app.config['SERVER_URL']}/",
            'publicKeyPem': g.site.public_key
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    resp.headers.set('Cache-Control', 'public, max-age=30')
    resp.headers.set('Vary', 'Accept')
    return resp


# instance actor (literally uses the word 'actor' without the /u/)
# required for interacting with instances using 'secure mode' (aka authorized fetch)
@bp.route('/actor', methods=['GET'])
def instance_actor():
    application_data = {
        '@context': default_context(),
        'type': 'Application',
        'id': f"{current_app.config['SERVER_URL']}/actor",
        'preferredUsername': f"{current_app.config['SERVER_NAME']}",
        # Castopod stores a follower from `name` without checking it exists, and refuses the
        # Follow when it is absent (interop D24: the instance actor follows synced podcasts)
        'name': f"{current_app.config['SERVER_NAME']}",
        'url': f"{current_app.config['SERVER_URL']}/about",
        'manuallyApprovesFollowers': True,
        'inbox': f"{current_app.config['SERVER_URL']}/actor/inbox",
        'outbox': f"{current_app.config['SERVER_URL']}/actor/outbox",
        'publicKey': {
            'id': f"{current_app.config['SERVER_URL']}/actor#main-key",
            'owner': f"{current_app.config['SERVER_URL']}/actor",
            'publicKeyPem': g.site.public_key
        },
        'endpoints': {
            'sharedInbox': f"{current_app.config['SERVER_URL']}/inbox",
        }
    }
    resp = jsonify(application_data)
    resp.content_type = 'application/activity+json'
    return resp


@bp.route('/service_worker.js', methods=['GET'])
def service_worker():
    js_path = os.path.join('static', 'service_worker.js')
    response = make_response(send_file(js_path, mimetype='text/javascript'))
    response.headers['Cache-Control'] = 'public, max-age=86400'  # cache for 1 day
    return response


# intercept requests for the PWA manifest.json and provide platform specific ones
@bp.route('/manifest.json', methods=['GET'])
@bp.route('/static/manifest.json', methods=['GET'])
def static_manifest():
    g.site = db.session.get(Site, 1)
    def get_manifest_for_os(os_family):
        base_dir = 'app/static/pwa_manifests'
        if os_family == 'mac os x':
            path = os.path.join(base_dir, 'ios', 'manifest.json')
        else:
            path = os.path.join(base_dir, os_family, 'manifest.json')
        return path if os.path.exists(path) else os.path.join(base_dir, 'default', 'manifest.json')

    try:
        res = uaparse(request.user_agent.string)
        manifest_path = get_manifest_for_os(res.os.family.lower())
    except Exception:
        manifest_path = os.path.join('app/static/pwa_manifests/default/manifest.json')

    with open(manifest_path, 'r') as f:
        manifest = json.load(f)

    # Modify manifest
    manifest['id'] = f'{current_app.config["SERVER_URL"]}'
    manifest['name'] = g.site.name if g.site.name else 'PieFed'
    manifest['description'] = g.site.description if g.site.description else ''
    
    # Update icons to use custom logos with fallbacks
    logo_512 = get_setting('logo_512', '')
    logo_192 = get_setting('logo_192', '')
    
    # Update the icons array
    for icon in manifest.get('icons', []):
        if icon.get('sizes') == '192x192':
            icon['src'] = logo_192 if logo_192 else '/static/images/piefed_logo_icon_t_192.png'
        elif icon.get('sizes') == '512x512':
            icon['src'] = logo_512 if logo_512 else '/static/images/piefed_logo_icon_t_512.png'

    # Build response with cache headers
    response = make_response(jsonify(manifest))
    # Cache for 1 hour on the client, prevent public/shared caching because we detect the user agent
    response.headers['Cache-Control'] = 'private, max-age=3600'

    return response


@bp.route('/feeds', methods=['GET', 'POST'])
@login_required_if_private_instance
def list_feeds():
    # default to no public feeds
    server_has_feeds = False
    search_param = request.args.get('search', '')

    if search_param == '':
        # find all the feeds marked as public
        public_feeds = feed_tree_public()
        
    else:
        # find all the feeds marked as public that match the search param
        public_feeds = feed_tree_public(search_param)

    if len(public_feeds) > 0:
        server_has_feeds = True

    # respond with json collection of public feeds for curl/AP requests
    if is_activitypub_request():
        site_data = lemmy_site_data()
        feeds_list = []
        for f in public_feeds:
            if f['feed'].is_local():
                feeds_list.append(f"{current_app.config['SERVER_URL']}/f/{f['feed'].machine_name}")
        site_data['site_view']['public_feeds'] = feeds_list
        site_data['site_view']['counts']['public_feeds'] = len(feeds_list)
        resp = jsonify(site_data)
        resp.content_type = 'application/activity+json'
        return resp
    else:
        # render the page
        return render_template('feed/public_feeds.html', server_has_feeds=server_has_feeds,
                               public_feeds_list=public_feeds,
                               subscribed_feeds=subscribed_feeds(current_user.get_id()),
                               search_hint=search_param)


@bp.route('/explore')
@login_required_if_private_instance
def explore():
    topics = topic_tree()
    return render_template('explore.html', topics=topics, menu_instance_feeds=menu_instance_feeds(),
                           menu_my_feeds=menu_my_feeds(current_user.id) if current_user.is_authenticated else None,
                           menu_subscribed_feeds=menu_subscribed_feeds(current_user.id) if current_user.is_authenticated else None,)


# RSS feed of the community
@bp.route('/index/feed', methods=['GET'])
@bp.route('/index/feed/<feed_type>', methods=['GET'])
#@cache.cached(timeout=600, query_string=True)
def index_rss(feed_type=None):

    # Refuse first, then answer conditionally. The 304 used to be computed
    # ABOVE this check, so a caller holding an ETag from before the instance
    # was made private -- or one guessed, since it is `home_{hash(last_active)}`
    # -- got `304 Not Modified` where a fresh request got 404. That is an
    # access check a conditional request walks past, and it is the same defect
    # this campaign fixed in app/community/routes.py's community feed.
    # R219: on a private instance a member's RSS token opens the feed; anyone else still gets 404
    user = rss_token_user()
    if g.site.private_instance and user is None:
        abort(404)

    # If nothing has changed since their last visit, return HTTP 304
    current_etag = f"home_{hash(g.site.last_active)}"
    if request_etag_matches(current_etag):
        return return_304(current_etag, 'application/rss+xml')

    # D1356: `rss_token_user` refuses a banned or deleted account's token -- three of the four
    # conditions `authorise_api_user` applies to a JWT; `verified` is deliberately not one,
    # since an instance with email verification off has legitimate unverified accounts.
    current_user_is_authenticated = user is not None
    rss_token = request.args.get('token')

    community_ids = [-1]
    low_quality_filter = 'AND c.low_quality is false' if current_user_is_authenticated and user.hide_low_quality else ''
    if current_user_is_authenticated:
        pc = community_membership_private(user.id)
        if len(pc) == 0:  # tuples must have at least 2 elements, I think?
            private_communities = tuple([0, 0])
        else:
            private_communities = tuple(pc + [0])
    else:
        private_communities = ()
    if len(private_communities) == 0:
        private_communities = tuple([0, 0])

    if feed_type is None:
        feed_type = 'local'

    # D1419. The chain used to open `if feed_type == 'subscribed' and ...` followed by
    # `elif feed_type == 'local' or not current_user_is_authenticated:`, so the second arm
    # swallowed EVERY feed type a reader without a token asked for. `/index/feed/popular`
    # and `/index/feed/all` both answered with the local feed while still titling
    # themselves 'Popular' and 'All', and the anonymous branch of the popular query below
    # was unreachable code that had never run.
    #
    # Ordering by `feed_type` first, and letting the authentication test decide only which
    # SQL each type uses, gives a reader the feed they named. `subscribed` is the one type
    # that genuinely requires a token, so it still falls through to `local` without one.
    if feed_type == 'subscribed' and current_user_is_authenticated:
        community_ids = db.session.execute(text(
            'SELECT id FROM community as c INNER JOIN community_member as cm ON cm.community_id = c.id WHERE cm.is_banned is false AND cm.user_id = :user_id'),
                                           {'user_id': user.id}).scalars()
    elif feed_type == 'popular':
        if not current_user_is_authenticated:
            community_ids = db.session.execute(
                text('SELECT id FROM community as c WHERE c.show_popular is true and c.private is false AND c.low_quality is false')).scalars()
        else:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE (c.private is false OR c.id IN {private_communities}) AND c.show_popular is true {low_quality_filter}')).scalars()
    elif feed_type == 'all':
        community_ids = [-1]  # Special value to indicate 'All'
    elif feed_type == 'local' or feed_type == 'subscribed':
        if not current_user_is_authenticated:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE c.private is false and c.instance_id = 1 {low_quality_filter}')).scalars()
        else:
            community_ids = db.session.execute(
                text(f'SELECT id FROM community as c WHERE (c.private is false OR c.id IN {private_communities}) AND c.instance_id = 1 {low_quality_filter}')).scalars()

    community_ids = list(community_ids)

    post_ids = get_deduped_post_ids(gibberish(15), community_ids, 'new',
                                    include_following=feed_type == 'subscribed' and current_user_is_authenticated)
    post_ids = paginate_post_ids(post_ids, 0, page_length=50)
    posts = post_ids_to_models(post_ids, 'new')

    description = shorten_string(g.site.description, 150) if g.site.description else None
    og_image = g.site.logo if g.site.logo else None
    fg = FeedGenerator()
    fg.id(f"{current_app.config['SERVER_URL']}/{feed_type}{rss_token}")
    fg.title(f'{g.site.name} - {feed_type.capitalize()}')
    fg.link(href=f"{current_app.config['SERVER_URL']}", rel='alternate')
    if og_image:
        fg.logo(og_image)
    else:
        fg.logo(f"{current_app.config['SERVER_URL']}/static/images/apple-touch-icon.png")
    if description:
        fg.subtitle(description)
    else:
        fg.subtitle(' ')
    fg.link(href=f"{current_app.config['SERVER_URL']}/feed", rel='self')
    fg.language('en')

    for post in posts:
        fe = fg.add_entry()
        fe.title(post.title)
        if post.slug:
            fe.link(href=f"{current_app.config['SERVER_URL']}{post.slug}")
        else:
            fe.link(href=f"{current_app.config['SERVER_URL']}/post/{post.id}")
        if post.url and not post.content_warning:
            type = mimetype_from_url(post.url)
            if type and not type.startswith('text/'):
                fe.enclosure(post.url, type=type)
        fe.description(feed_entry_body(post))
        fe.guid(post.profile_id(), permalink=True)
        fe.author(name=post.author.user_name)
        fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))

    response = make_response(fg.rss_str())
    response.headers.set('Content-Type', 'application/rss+xml')
    response.headers.add_header('ETag', f"home_{hash(g.site.last_active)}")
    response.headers.add_header('Cache-Control', 'no-cache, max-age=600, must-revalidate')
    return response


@bp.route('/r/random')
@bp.route('/random')
@login_required_if_private_instance
def random():
    if blocked := blocked_or_banned_instances(current_user.get_id()):
        sql = """select c.id from "community" c
                inner join instance i on c.instance_id = i.id
                where c.banned is false and i.gone_forever is false and c.post_count > 0 and c.private is false
                and i.id not in :blocked_instances and c.nsfw is false 
                order by random()
                limit 1"""
        community_id = db.session.execute(text(sql), {'blocked_instances': tuple(blocked)}).scalar_one_or_none()
    else:
        sql = """select c.id from "community" c
                        inner join instance i on c.instance_id = i.id
                        where c.banned is false and i.gone_forever is false and c.post_count > 0 and c.private is false
                        order by random()
                        limit 1"""
        community_id = db.session.execute(text(sql)).scalar_one_or_none()
    if community_id:
        community = db.session.get(Community, community_id)
        flash(Markup(_('<a href="/r/random">Try another random community</a>')))
        return redirect(url_for('activitypub.community_profile', actor=community.link()))
    else:
        return render_template('generic_message.html', title=_('Sorry'), message=_('No communities found.'))


@bp.route('/r/randnsfw')
@bp.route('/r/randomnsfw')
@bp.route('/randomnsfw')
@login_required_if_private_instance
def random_nsfw():
    if blocked := blocked_or_banned_instances(current_user.get_id()):
        sql = """select c.id from "community" c
                inner join instance i on c.instance_id = i.id
                where c.banned is false and i.gone_forever is false and c.nsfw is true and c.post_count > 0 and c.private is false
                and i.id not in :blocked_instances
                order by random()
                limit 1"""
        community_id = db.session.execute(text(sql), {'blocked_instances': tuple(blocked)}).scalar_one_or_none()
    else:
        sql = """select c.id from "community" c
                inner join instance i on c.instance_id = i.id
                where c.banned is false and i.gone_forever is false and c.nsfw is true and c.post_count > 0 and c.private is false
                order by random()
                limit 1"""
        community_id = db.session.execute(text(sql)).scalar_one_or_none()
    if community_id:
        community = db.session.get(Community, community_id)
        flash(Markup(_('<a href="/r/randnsfw">Try another random community</a>')))
        return redirect(url_for('activitypub.community_profile', actor=community.link()))
    else:
        return render_template('generic_message.html', title=_('Sorry'), message=_('No communities found.'))


@bp.route('/content_warning')
def content_warning():
    next_url = referrer()
    message = """
    This website contains age-restricted materials including nudity and explicit depictions of sexual activity.

    By entering, you affirm that you are at least 18 years of age or the age of majority in the jurisdiction you are accessing the website from and you consent to viewing sexually explicit content.
    """
    resp = make_response(render_template('content_warning.html', title=_('Content warning'), message=message, next_url=next_url))
    resp.headers.set('Vary', 'Accept, Cookie, Accept-Language')
    resp.headers.set('Cache-Control', 'private, max-age=1, must-revalidate')
    return resp


@bp.route('/anoobis')
def anoobis():
    next = request.args.get('next')
    if next is None:
        return ''
    # D1359. This was a second implementation of the origin check, and a weaker
    # one: `f.host is None` accepts everything furl reads as having no authority,
    # and a browser does not agree with furl about what that means. The template
    # puts this value in `location.href`, so each of these was an open redirect on
    # a page whose whole job is to bounce an anonymous visitor onward -- measured
    # against furl:
    #
    #   \\evil.test/x      host=None          browsers fold \ to /, so this is
    #                                          //evil.test/x -- protocol-relative
    #   /\evil.test        host=None          the same, as /\ -> //
    #   https:/\evil.test  host=None, https   -> https://evil.test
    #   http:evil.test     host=None, http    scheme-relative; Chrome resolves it
    #                                          as http://evil.test/
    #
    # `is_safe_redirect_target` is THE origin check -- `back()` and all three of
    # `referrer()`'s sources already go through it, and its own docstring names the
    # back()/referrer() divergence that having two implementations produced. It
    # rejects all four, accepts a relative path and this server's own host, and
    # honours the admin's `redirect_policy` as every other redirect does.
    if next and is_safe_redirect_target(next):
        return render_template('anoobis.html', next=next, diff_desktop=current_app.config['ANOOBIS_DIFFICULTY_DESKTOP'],
                               diff_mobile=current_app.config['ANOOBIS_DIFFICULTY_MOBILE'])
    else:
        # abort(403), which is what the line below the raise always meant. The
        # raise turned a refused open-redirect attempt -- the guard working --
        # into a 500 and a Sentry event, with the attacker's own host echoed
        # into the message, and left the abort unreachable behind it.
        abort(403)


@bp.route('/bot_challenge/<uuid>')
@limiter.limit("20 per 1 minutes")
def bot_challenge_result(uuid):
    challenge = BotChallenge.query.filter(BotChallenge.uuid == uuid).first()
    if challenge:
        if challenge.is_a_bot is True:
            return render_template('generic_message.html', title=_('Sorry'),
                                   message=_("You took too long to respond so your account has been flagged as a bot."))
        else:
            challenge.is_a_bot = False
            db.session.commit()
            return render_template('generic_message.html', title=_('Thanks!'),
                                   message=_("You have confirmed that your account is operated by a human."))
    else:
        abort(404)


@bp.route('/my-year-in-review/<year>')
@login_required
def my_year_in_review(year):
    return render_template('generic_message.html', title=_('This page is intentionally left blank.'), message=_("We don't track you, so there's not much data to make graphs of."))


@bp.route('/webhook', methods=['POST'])
@limiter.limit("60 per 1 minutes", methods=['POST'])
def receive_webhook():
    # The plugins act on whatever arrives here, so only a caller holding WEBHOOK_SECRET may reach them
    secret = current_app.config['WEBHOOK_SECRET']
    if not secret:
        abort(404)
    if not hmac.compare_digest(request.headers.get('X-Webhook-Secret', '').encode(), secret.encode()):
        abort(403)

    payload = request.get_json()

    if not payload:
        return jsonify({"error": "no payload received"}), 400
    
    plugins.fire_hook("webhook", payload)

    return '', 202


@bp.route('/health', methods=['HEAD', 'GET'])
def health():
    return 'Ok'


@bp.route('/health2', methods=['GET', 'HEAD'])
def health2():
    # Do some DB access to provide a picture of the performance of the instance
    # This is all busy-work to give an indication to the caller of the instance performance so there is a lot of # noqa comments to silence ruff.

    search_param = request.args.get('search', '')
    # D1312, the same three reads as :289 -- and this endpoint takes no login.
    topic_id = request.args.get('topic_id', 0, type=int)
    feed_id = request.args.get('feed_id', 0, type=int)
    language_id = request.args.get('language_id', 0, type=int)
    nsfw = request.args.get('nsfw', None)
    sort_by = request.args.get('sort_by', 'post_reply_count desc')

    if not g.site.enable_nsfw:
        nsfw = None
    else:
        if nsfw is None:
            nsfw = 'all'

    if request.args.get('prompt'):
        flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))

    topics = Topic.query.order_by(Topic.name).all()              # noqa f841
    languages = Language.query.order_by(Language.name).all()     # noqa f841
    communities = Community.query.filter_by(banned=False)
    if search_param == '':
        pass
    else:
        communities = communities.filter(
            or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))

    if topic_id != 0:
        communities = communities.filter_by(topic_id=topic_id)

    if language_id != 0:
        communities = communities.join(community_language).filter(community_language.c.language_id == language_id)

    # find all the feeds marked as public
    public_feeds = Feed.query.filter_by(public=True).order_by(Feed.title).all() # noqa f841

    # if filtering by public feed
    # get all the ids of the communities
    # then filter the communites to ones whose ids match the feed
    if feed_id != 0:
        # D1394, as list_communities:411. This endpoint discards its rows, so
        # there is nothing to read out of it -- but it takes the same parameter
        # from the same untrusted place, and the two bodies are meant to be the
        # same work.
        feed_community_ids = []
        if feed_readable_by(db.session.get(Feed, feed_id), current_user.id if current_user.is_authenticated else None):
            for item in FeedItem.query.filter_by(feed_id=feed_id).all():
                feed_community_ids.append(item.community_id)
        communities = communities.filter(Community.id.in_(feed_community_ids))

    if current_user.is_authenticated:
        if current_user.hide_low_quality:
            communities = communities.filter(Community.low_quality == False)
        banned_from = communities_banned_from(current_user.id)
        if banned_from:
            communities = communities.filter(Community.id.not_in(banned_from))
        if current_user.hide_nsfw == 1:
            nsfw = None
            communities = communities.filter(Community.nsfw == False)
        else:
            if nsfw == 'no':
                communities = communities.filter(Community.nsfw == False)
            elif nsfw == 'yes':
                communities = communities.filter(Community.nsfw == True)
        if current_user.hide_nsfl == 1:
            communities = communities.filter(Community.nsfl == False)
        instance_ids = blocked_or_banned_instances(current_user.id)
        if instance_ids:
            communities = communities.filter(
                or_(Community.instance_id.not_in(instance_ids), Community.instance_id == None))
        filtered_out_community_ids = filtered_out_communities(current_user)
        if len(filtered_out_community_ids):
            communities = communities.filter(Community.id.not_in(filtered_out_community_ids))

    else:
        communities = communities.filter(Community.nsfl == False)
        if nsfw == 'no':
            communities = communities.filter(and_(Community.nsfw == False))
        elif nsfw == 'yes':
            communities = communities.filter(and_(Community.nsfw == True))

    communities = communities.order_by(safe_order_by(sort_by, Community,
                                                     {'title', 'subscriptions_count', 'post_count', 'post_reply_count',
                                                      'last_active', 'created_at'})).limit(100)

    c = communities.all()   # noqa f841

    return ''
