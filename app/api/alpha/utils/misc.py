import re

from urllib.parse import urlparse

from flask import current_app, g
from sqlalchemy import desc

from app.activitypub.util import find_actor_or_create, remote_object_to_json, actor_json_to_model, \
    find_community, create_resolved_object, normalise_actor_string, host_of
from app.api.alpha.utils.community import get_community_list
from app.api.alpha.utils.post import get_post_list
from app.api.alpha.utils.user import get_user_list
from app.api.alpha.utils.reply import get_reply_list
from app.api.alpha.views import search_view, post_view, reply_view, user_view, community_view, feed_view, \
    neutral_person, reply_removal_ack_view
from app.community.util import search_for_community
from app.discovery.podcast import PODCAST_DROP, podcast_route_for, podcast_twin_named_by_other
from app.models import Post, PostReply, User, Community, BannedInstances, Feed, ModLog
from app.user.utils import search_for_user
from app.visibility import can_view, modlog_open_clause
from app.feed.util import search_for_feed
from app.utils import authorise_api_user, gibberish, subscribed_feeds, communities_banned_from, \
    moderating_communities_ids, joined_or_modding_communities, blocked_communities, blocked_or_banned_instances
from app import db


def get_search(auth, data):
    # D1189. The guard was `'q' not in data and 'type_' not in data`, so
    # EITHER key satisfied it -- and `data['type_']` is read on the next line.
    # A search carrying only `q` was `KeyError: 'type_'`, measured as PROBE
    # bj1. The route's schema marks both required, so this is what a direct
    # caller hits; the guard now asks for what the function uses.
    if not data or 'q' not in data or 'type_' not in data:
        raise Exception('missing parameters for search')

    type = data['type_']
    listing_type = data['listing_type'] if 'listing_type' in data else 'All'

    data['type_'] = listing_type

    search_type = 'Posts' if type == 'Url' else type
    search_json = search_view(search_type)
    if type == 'Communities':
        search_json['communities'] = get_community_list(auth, data)['communities']
    elif type == 'Posts' or type == 'Url':
        search_json['posts'] = get_post_list(auth, data, search_type=type)['posts']
    elif type == 'Users':
        search_json['users'] = get_user_list(auth, data)['users']
    elif type == 'Comments':
        search_json['comments'] = get_reply_list(auth, data)['comments']

    return search_json


def feed_view_arguments(user_id):
    """Everything `feed_view` needs beyond the feed itself.

    D1192. This was built inline, and only when the QUERY looked like a feed
    (`/f/` in it, or a leading `~`) -- while three `feed_view(feed=object,
    **feed_dict)` calls below can be reached by a query of any shape, because
    what a lookup ANSWERS with is not decided by how it was addressed. A
    `/m/` url that resolves to a feed was `TypeError: feed_view() argument
    after ** must be a mapping, not NoneType`, measured as PROBE bl1. The
    arguments depend on the caller, not the query, so they are built where
    they are needed.
    """
    arguments = {"variant": 2, "user_id": user_id, "include_communities": False}
    if user_id:
        g.user = db.session.get(User, user_id)
        arguments["blocked_community_ids"] = blocked_communities(user_id)
        arguments["blocked_instance_ids"] = blocked_or_banned_instances(user_id)
        arguments["subscribed"] = subscribed_feeds(user_id)
        arguments["banned_from"] = communities_banned_from(user_id)
        arguments["communities_moderating"] = moderating_communities_ids(user_id)
        arguments["communities_joined"] = joined_or_modding_communities(user_id)
    else:
        arguments["subscribed"] = []
        arguments["banned_from"] = []
        arguments["communities_moderating"] = []
        arguments["communities_joined"] = []
        arguments["blocked_community_ids"] = []
        arguments["blocked_instance_ids"] = []
    return arguments


def _own_host_author(ap_json):
    """The object's attributedTo actor id when it is on the host the object was fetched from -- the author
    resolve_remote_post hands find_community (D24 R2) -- or None. The attributedTo walk is create_resolved_object's."""
    attributed_to = ap_json.get('attributedTo')
    if isinstance(attributed_to, dict):
        attributed_to = [attributed_to]
    if isinstance(attributed_to, list):
        first = next((a for a in attributed_to if isinstance(a, str) or
                      (isinstance(a, dict) and a.get('type') in ('Person', 'Podcast'))), None)
        attributed_to = first.get('id') if isinstance(first, dict) else first
    if isinstance(attributed_to, str) and host_of(attributed_to) and host_of(attributed_to) == host_of(ap_json['id']):
        return attributed_to
    return None


def get_resolve_object(auth, data, user_id=None, recursive=False):
    if not data or 'q' not in data:
        raise Exception('missing q parameter for resolve_object')
    if auth:
        user_id = authorise_api_user(auth)
    elif user_id is None:
        # PERM-1: resolving can fetch and store remote content, so the API
        # requires an authenticated caller; in-app callers pass user_id.
        raise Exception('incorrect_login')

    query = data['q']

    # Check if this is a request for a feed, then define some boilerplate for all subsequent feed_view calls
    if "/f/" in query or query.startswith("~"):
        feed_dict = feed_view_arguments(user_id)
    else:
        feed_dict = None

    # Check to see if the query parameter is already federated and is the canonical ap url
    object = db.session.query(PostReply).filter_by(ap_id=query).first()
    if object:
        if object.deleted or (not recursive and not can_view(object, user_id)):
            raise Exception('No object found.')
        return reply_view(reply=object, variant=5, user_id=user_id) if not recursive else object
    object = db.session.query(Post).filter_by(ap_id=query).first()
    if object:
        if object.deleted or (not recursive and not can_view(object, user_id)):
            raise Exception('No object found.')
        return post_view(post=object, variant=5, user_id=user_id) if not recursive else object
    object = db.session.query(Community).filter_by(ap_profile_id=query.lower()).first()
    if object:
        if object.banned:
            raise Exception('No object found.')
        return community_view(community=object, variant=6, user_id=user_id) if not recursive else object
    object = db.session.query(User).filter_by(ap_profile_id=query.lower()).first()
    if object:
        if object.deleted or object.banned:
            raise Exception('No object found.')
        return user_view(user=object, variant=7, user_id=user_id) if not recursive else object
    object = db.session.query(Feed).filter_by(ap_profile_id=query.lower()).first()
    if object:
        if object.banned:
            raise Exception('No object found.')
        return feed_view(feed=object, **(feed_dict or feed_view_arguments(user_id))) \
            if not recursive else object

    # if not found and user is logged in, fetch the object if it's not hosted on a banned instance
    # note: accommodating !, @, and ~ queries for communities, people, and feeds is different from lemmy's v3 api

    server = None
    if query.startswith('https://') or query.startswith('http://'):
        parsed_url = urlparse(query)
        server = parsed_url.netloc.lower()
    elif query.startswith('!') or query.startswith('@') or query.startswith('~'):
        name, server = normalise_actor_string(query)

    if not server:  # can't find server
        raise Exception('No object found.')
    
    if server == current_app.config['SERVER_NAME']:
        local_request = True
    else:
        if query.startswith('!'):
            # Check if this community is already federated
            local_request = bool(search_for_community(query.lower(), allow_fetch=False))
        elif query.startswith('@'):
            # Check if this user is already federated
            if query.endswith(current_app.config['SERVER_NAME']):
                user_name = query[1:]
                user_name = user_name.split('@')[0]
                local_request = bool(search_for_user(user_name.lower(), allow_fetch=False))
            else:
                local_request = bool(search_for_user(query.lower(), allow_fetch=False))
        elif query.startswith('~'):
            # Check if this feed is already federated
            local_request = bool(search_for_feed(query.lower(), allow_fetch=False))
        else:
            local_request = False
    
    if not user_id and not local_request:  # not logged in
        raise Exception('No object found.')

    banned = db.session.query(BannedInstances).filter_by(domain=server).first()
    if banned:
        raise Exception('No object found.')
    
    # Request for something on the local instance already, check local db instead of fetching remote info
    if local_request:

        # Communities
        # D1190. This read
        #
        #     (query.startswith('!')) or
        #     (('/c/' in query) and ('/p/' not in query)) or
        #     (('/m/' in query) and ('/t/' not in query)) and
        #     ('/comment/' not in query)
        #
        # and `and` binds tighter than `or`, so the comment exclusion applied
        # to the THIRD disjunct alone. A comment url that names its community
        # -- `https://<server>/c/<name>/comment/<id>`, which is the shape
        # PieFed's own comment permalinks take -- was resolved as the
        # COMMUNITY. Measured: `PROBE bj2 outcome: ['community']`, against
        # `PROBE bj3 outcome: ['comment']` for the same comment addressed
        # without one.
        if ('/comment/' not in query) and (
            (query.startswith('!')) or
            (('/c/' in query) and ('/p/' not in query)) or
            (('/m/' in query) and ('/t/' not in query))):
            # This is a community specified using !communtiy@instance.tld notation
            if query.startswith('!'):
                object = search_for_community(query.lower(), allow_fetch=bool(user_id))
                if object:
                    return community_view(community=object, variant=6, user_id=user_id)
                else:
                    raise Exception('No object found.')
            
            # This is a community specified by url
            # D1193. A `try: ... except: comm_name = None` and an
            # `if not comm_name: raise` stood here, and neither could be
            # taken: this branch is reached only when `/c/` or `/m/` is in
            # the query, and `r"/[cm]/(.*?)(/|$)"` always matches such a
            # query -- the group can be empty, so `"!" + group(1)` is at
            # worst `"!"`, which is truthy. The same pair stood in the user
            # and feed branches below, on the same reasoning.
            comm_pattern = re.compile(r"/[cm]/(.*?)(/|$)")
            matches = re.search(comm_pattern, query)
            comm_name = "!" + matches.group(1)

            if "@" not in comm_name:
                comm_name = comm_name + "@" + current_app.config['SERVER_NAME']

            object = search_for_community(comm_name.lower(), allow_fetch=bool(user_id))

            if object:
                return community_view(community=object, variant=6, user_id=user_id)
            else:
                raise Exception('No object found.')

        # Users
        if query.startswith("@") or "/u/" in query:

            # This is a user specified using the @user@instance.tld notation
            if query.startswith("@"):
                if query.endswith(current_app.config['SERVER_NAME']):
                    user_name = query[1:]
                    user_name = user_name.split('@')[0]
                    object = search_for_user(user_name.lower(), allow_fetch=bool(user_id))
                else:
                    object = search_for_user(query.lower(), allow_fetch=bool(user_id))
                if object:
                    return user_view(user=object, variant=7, user_id=user_id)
                else:
                    raise Exception('No object found.')
            
            # This is a user specified by url
            user_pattern = re.compile(r"/u/(.*?)(/|$)")  # D1193, as above
            matches = re.search(user_pattern, query)
            user_name = matches.group(1)

            if user_name.endswith(current_app.config['SERVER_NAME']) and '@' in user_name:
                user_name = user_name.split('@')[0]

            object = search_for_user(user_name.lower(), allow_fetch=bool(user_id))
            if object:
                return user_view(user=object, variant=7, user_id=user_id)
            else:
                raise Exception('No object found.')
        
        # Posts
        post_patterns = ["/post/", "/p/", "/t/"]
        if any(pattern in query for pattern in post_patterns) and "/comment/" not in query:

            if "/post/" in query:
                # Post url from lemmy or older piefed url format
                post_pattern = re.compile(r"/post/(\d*)(/|$)")
            elif "/p/" in query:
                # Post url from more recent piefed versions
                post_pattern = re.compile(r"/p/(\d*)(/|$)")
            elif "/t/" in query:
                # Post url from mbin
                post_pattern = re.compile(r"/t/(\d*)(/|$)")

            # D1193's fourth site: an `else: post_pattern = None` with an
            # `if not post_pattern: raise` under it, where this branch is
            # reached only when one of the three patterns is in the query.
            # The `except` below it IS reachable -- `/post/notanumber` does
            # not match -- and stays.

            # Do the regex
            matches = re.search(post_pattern, query)
            try:
                post_id = matches.group(1)
            except:
                post_id = None
                
            if not post_id:
                raise Exception('No object found.')
            
            # Since this is a local request, just search for the post by id
            object = db.session.get(Post, post_id)

            # A followers-only post is as unknown to this caller as an id nobody holds
            if not object or not can_view(object, user_id):
                raise Exception('No object found.')
            else:
                return post_view(post=object, variant=5, user_id=user_id)
        
        # Comments
        if "/comment/" in query:
            # Do the regex
            comment_pattern = re.compile(r"/comment/(\d*)(/|$)")
            matches = re.search(comment_pattern, query)
            try:
                comment_id = matches.group(1)
            except:
                comment_id = None
            
            if not comment_id:
                raise Exception('No object found.')
            
            # Since this is a local request, just search for the comment by id
            object = db.session.get(PostReply, comment_id)

            if not object or not can_view(object, user_id):
                raise Exception('No object found.')
            else:
                return reply_view(reply=object, variant=5, user_id=user_id)
        
        # Feeds
        if "/f/" in query or query.startswith("~"):
            if query.startswith("~"):
                # This feed is specified using ~ notation
                object = search_for_feed(query.lower(), allow_fetch=False)
                
                if object and feed_dict:
                    return feed_view(feed=object, **feed_dict)
                else:
                    raise Exception('No object found.')
            
            # This is a feed specified by url
            feed_pattern = re.compile(r"/[f]/(.*?)($)")  # D1193, as above
            matches = re.search(feed_pattern, query)
            feed_name = "~" + matches.group(1)

            if "@" not in feed_name:
                feed_name = feed_name + "@" + current_app.config['SERVER_NAME']
            
            object = search_for_feed(feed_name, allow_fetch=bool(user_id))

            if object and feed_dict:
                return feed_view(feed=object, **feed_dict)
            else:
                raise Exception('No object found.')

    # use hints first in query first
    # assume that queries starting with ! are for a community
    if not recursive and query.startswith('!'):
        object = search_for_community(query.lower())
        if object:
            return community_view(community=object, variant=6, user_id=user_id)
    # assume that queries starting with @ are for a user
    if not recursive and query.startswith('@'):
        object = search_for_user(query.lower())
        if object:
            return user_view(user=object, variant=7, user_id=user_id)
    # assume that queries starting with ~ are for a feed
    if not recursive and query.startswith('~'):
        object = search_for_feed(query.lower())
        if object and feed_dict:
            return feed_view(feed=object, **feed_dict)
    # if the instance is following the lemmy convention, a '/u/' means user and '/c/' means community
    if '/u/' in query or (('/comment/' not in query) and (  # D1190's twin
        (query.startswith('!')) or
        (('/c/' in query) and ('/p/' not in query)) or
        (('/m/' in query) and ('/t/' not in query)))):
        object = find_actor_or_create(query.lower())
        if object:
            if isinstance(object, User):
                return user_view(user=object, variant=7, user_id=user_id) if not recursive else object
            elif isinstance(object, Community):
                return community_view(community=object, variant=6, user_id=user_id) if not recursive else object
            elif isinstance(object, Feed):
                return feed_view(feed=object,
                                 **(feed_dict or feed_view_arguments(user_id)))

    # no more hints from query
    ap_json = remote_object_to_json(query)
    if not ap_json:
        raise Exception('No object found.')
    if not 'id' in ap_json:
        raise Exception('No object found.')
    if query != ap_json['id']:
        # query URL doesn't match original author's URL, so call this function with that URL instead
        # 'recursive' is (incorrectly) set to False to get the view, not the object
        return get_resolve_object(None, {"q": ap_json['id']}, user_id, False)

    # a user or a community
    if not 'type' in ap_json:
        raise Exception('No object found.')

    # D1191. This read `... == 'Person' or ... == 'Service' or ... == 'Group'
    # or ... == 'Feed' and 'preferredUsername' in ap_json`, and `and` binds
    # tighter than `or`, so the membership test guarded the **Feed** arm
    # alone -- while the line below reads the key for all four. An actor
    # document of any other type carrying no `preferredUsername` was
    # `KeyError: 'preferredUsername'`, measured as PROBE bk1. D1190's shape,
    # second instance in this file.
    if (ap_json['type'] in ('Person', 'Service', 'Podcast', 'Group', 'Feed')
            and 'preferredUsername' in ap_json):
        name = ap_json['preferredUsername'].lower()
        object = actor_json_to_model(ap_json, name, server)
        if object:
            if isinstance(object, User):
                return user_view(user=object, variant=7, user_id=user_id) if not recursive else object
            elif isinstance(object, Community):
                return community_view(community=object, variant=6, user_id=user_id) if not recursive else object
            elif isinstance(object, Feed):
                return feed_view(feed=object,
                                 **(feed_dict or feed_view_arguments(user_id)))

    # a post or a reply
    author = _own_host_author(ap_json)
    community = find_community(ap_json, author=author)  # D24 R2: a podcast's own episode names its twin community
    if not community and not ap_json.get('inReplyTo') and author:
        # D24: a top-level episode from a Castopod podcast belongs in the podcast's community, as in the inbox
        route = podcast_route_for(db.session.query(User).filter_by(ap_profile_id=author.lower()).first())
        if route is PODCAST_DROP:   # a banned podcast's episode is dropped
            raise Exception('No object found.')
        community = route
    # if community doesn't already exist, call this function recursively to create it
    if not community:
        locations = ['audience', 'cc', 'to']
        for location in locations:
            if location in ap_json:
                potential_id = ap_json[location]
                if isinstance(potential_id, str):
                    if not potential_id.startswith('https://www.w3.org') and not potential_id.endswith('/followers'):
                        potential_community = get_resolve_object(None, {"q": potential_id}, user_id, True)
                        if isinstance(potential_community, Community) and \
                                not podcast_twin_named_by_other(potential_community, author):  # D24 R2
                            community = potential_community
                            break
                if isinstance(potential_id, list):
                    for c in potential_id:
                        if not c.startswith('https://www.w3.org') and not c.endswith('/followers'):
                            potential_community = get_resolve_object(None, {"q": c}, user_id, True)
                            if isinstance(potential_community, Community) and \
                                    not podcast_twin_named_by_other(potential_community, author):  # D24 R2
                                community = potential_community
                                break

        if not community and 'inReplyTo' in ap_json and ap_json['inReplyTo'] is not None:
            comment_being_replied_to = None
            post_being_replied_to = Post.get_by_ap_id(ap_json['inReplyTo'])
            if post_being_replied_to:
                community = post_being_replied_to.community
            else:
                comment_being_replied_to = PostReply.get_by_ap_id(ap_json['inReplyTo'])
                if comment_being_replied_to:
                    community = comment_being_replied_to.community
            # if parent doesn't already exist, call this function recursively to create it, and use parent's community
            if not post_being_replied_to and not comment_being_replied_to:
                object = get_resolve_object(None, {"q": ap_json['inReplyTo']}, user_id, True)
                if object:
                    community = object.community

        if not community:
            raise Exception('No object found.')

    # pretend this was Announced in, so an existing function can be re-used
    announce_id = f"https://{server}/activities/announce/{gibberish(15)}"
    object = create_resolved_object(query, ap_json, server, community, announce_id, False)
    # if object can't be created due to missing a parent post or reply, call this function recursively to create it.
    if not object:
        if 'inReplyTo' in ap_json and ap_json['inReplyTo'] is not None:
            get_resolve_object(None, {"q": ap_json['inReplyTo']}, user_id, True)
            object = create_resolved_object(query, ap_json, server, community, announce_id, False)

    if object and not recursive and not can_view(object, user_id):
        raise Exception('No object found.')  # as for an object that could not be resolved
    if object:
        if isinstance(object, Post):
            return post_view(post=object, variant=5, user_id=user_id) if not recursive else object
        elif isinstance(object, PostReply):
            return reply_view(reply=object, variant=5, user_id=user_id) if not recursive else object

    # failed to resolve if here.
    raise Exception('No object found.')


def get_suggestion(data):
    query = data['q']
    result = []
    if query.startswith('@'):
        if 'post_id' in data:
            people_from_post = User.query.join(PostReply, PostReply.user_id == User.id).\
                filter(PostReply.post_id == data['post_id']).\
                filter(User.user_name.ilike(f'{query[1:]}%')).distinct()

            for person in people_from_post.all():
                person_text = person.lemmy_link()
                if person_text not in result:
                    result.append(person_text)

        other_people = User.query.filter(User.user_name.ilike(f'{query[1:]}%')).order_by(desc(User.reputation)).limit(7).all()
        for other_person in other_people:
            if len(result) >= 7:
                break
            person_text = other_person.lemmy_link()
            if person_text not in result:
                result.append(person_text)
        if len(result) < 7:
            other_people = User.query.filter(User.user_name.ilike(f'%{query[1:]}%')).order_by(
                desc(User.reputation)).limit(7).all()
            for other_person in other_people:
                if len(result) >= 7:
                    break
                person_text = other_person.lemmy_link()
                if person_text not in result:
                    result.append(person_text)
    elif query.startswith('!'):
        for community in Community.query.filter(Community.name.ilike(f'{query[1:]}%')).order_by(desc(Community.active_monthly)).limit(7).all():
            result.append(community.lemmy_link()[1:])
    return {'result': result}


def get_modlog(auth, data):
    type_ = data['type_'] if 'type_' in data else "All"
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10
    mod_person_id = int(data['mod_person_id']) if 'mod_person_id' in data else 0
    community_id = int(data['community_id']) if 'community_id' in data else 0
    other_person_id = int(data['other_person_id']) if 'other_person_id' in data else 0
    post_id = int(data['post_id']) if 'post_id' in data else 0
    comment_id = int(data['comment_id']) if 'comment_id' in data else 0

    user = authorise_api_user(auth, return_type='model') if auth else None
    is_admin = user and (user.is_admin() or user.is_staff())

    # Build base query with common filters shared by all categories
    base_q = ModLog.query
    if not is_admin:
        base_q = base_q.filter(ModLog.public == True)
    if mod_person_id:
        base_q = base_q.filter(ModLog.user_id == mod_person_id)
    if community_id:
        base_q = base_q.filter(ModLog.community_id == community_id)
    if other_person_id:
        base_q = base_q.filter(ModLog.target_user_id == other_person_id)
        if not is_admin:
            base_q = base_q.filter(modlog_open_clause())  # R3: those entries name no target user to this reader
    if post_id:
        base_q = base_q.filter(ModLog.post_id == post_id)
    if comment_id:
        base_q = base_q.filter(ModLog.reply_id == comment_id)

    # Each category is (action_strings, community_id_required)
    # community_id_required: True = must have community_id, False = must not, None = don't care
    all_categories = {
        'ModRemovePost':       (['delete_post', 'restore_post'],           None),
        'ModLockPost':         (['lock_post', 'unlock_post'],              None),
        'ModFeaturePost':      (['featured_post', 'unfeatured_post'],      None),
        'ModRemoveComment':    (['delete_post_reply', 'restore_post_reply'], None),
        'ModRemoveCommunity':  (['delete_community'],                      None),
        'ModBanFromCommunity': (['ban_user', 'unban_user'],                True),
        'ModBan':              (['ban_user', 'unban_user'],                False),
        'ModAddCommunity':     (['add_mod', 'remove_mod'],                 True),
        'ModAdd':              (['add_mod', 'remove_mod'],                 False),
    }

    if type_ != 'All':
        if type_ not in all_categories:
            return _empty_modlog_response()
        categories_to_fetch = {type_: all_categories[type_]}
    else:
        categories_to_fetch = all_categories

    def fetch_category(actions, comm_required):
        q = base_q.filter(ModLog.action.in_(actions))
        if comm_required is True:
            q = q.filter(ModLog.community_id.isnot(None))
        elif comm_required is False:
            q = q.filter(ModLog.community_id.is_(None))
        return q.order_by(desc(ModLog.created_at)).paginate(
            page=page, per_page=limit, error_out=False).items

    fetched = {
        name: fetch_category(actions, comm_required)
        for name, (actions, comm_required) in categories_to_fetch.items()
    }

    viewer_id = user.id if user else None

    def names_hidden_content(entry):
        return any(obj is not None and not can_view(obj, None)
                   for obj in (entry.post, entry.reply, entry.reply.post if entry.reply else None))

    def build_removed_posts(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_remove_post': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'post_id': entry.post_id,
                    'reason': entry.reason,
                    'removed': entry.action == 'delete_post',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'post': post_view(entry.post, variant=1, user_id=viewer_id) if entry.post else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
            })
        return result

    def build_locked_posts(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_lock_post': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'post_id': entry.post_id,
                    'locked': entry.action == 'lock_post',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'post': post_view(entry.post, variant=1, user_id=viewer_id) if entry.post else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
            })
        return result

    def build_featured_posts(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_feature_post': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'post_id': entry.post_id,
                    'featured': entry.action == 'featured_post',
                    'is_featured_community': True,
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'post': post_view(entry.post, variant=1, user_id=viewer_id) if entry.post else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
            })
        return result

    def build_removed_comments(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_remove_comment': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'comment_id': entry.reply_id,
                    'reason': entry.reason,
                    'removed': entry.action == 'delete_post_reply',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'comment': (reply_view(entry.reply, variant=1) if can_view(entry.reply, viewer_id)
                            else reply_removal_ack_view(entry.reply)['comment']) if entry.reply else None,
                # R3: admins see who; anyone else, not for an entry about content that is not open
                'commenter': (user_view(entry.reply.user_id, variant=1)
                              if is_admin or not names_hidden_content(entry) else neutral_person())
                if entry.reply else None,
                'post': post_view(entry.reply.post_id, variant=1, user_id=viewer_id) if entry.reply else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
            })
        return result

    def build_removed_communities(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_remove_community': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'community_id': entry.community_id,
                    'reason': entry.reason,
                    'removed': True,
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
            })
        return result

    def build_banned_from_community(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_ban_from_community': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'other_person_id': entry.target_user_id,
                    'community_id': entry.community_id,
                    'reason': entry.reason,
                    'banned': entry.action == 'ban_user',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
                'banned_person': user_view(entry.target_user, variant=1) if entry.target_user else None,
            })
        return result

    def build_banned(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_ban': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'other_person_id': entry.target_user_id,
                    'reason': entry.reason,
                    'banned': entry.action == 'ban_user',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'banned_person': user_view(entry.target_user, variant=1) if entry.target_user else None,
            })
        return result

    def build_added_to_community(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_add_community': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'other_person_id': entry.target_user_id,
                    'community_id': entry.community_id,
                    'removed': entry.action == 'remove_mod',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'community': community_view(entry.community, variant=1) if entry.community else None,
                'modded_person': user_view(entry.target_user, variant=1) if entry.target_user else None,
            })
        return result

    def build_added(entries):
        result = []
        for entry in entries:
            result.append({
                'mod_add': {
                    'id': entry.id,
                    'mod_person_id': entry.user_id,
                    'other_person_id': entry.target_user_id,
                    'removed': entry.action == 'remove_mod',
                    'when_': entry.created_at.isoformat(timespec="microseconds") + 'Z',
                },
                'moderator': user_view(entry.author, variant=1) if entry.author else None,
                'modded_person': user_view(entry.target_user, variant=1) if entry.target_user else None,
            })
        return result

    return {
        'removed_posts':        build_removed_posts(fetched.get('ModRemovePost', [])),
        'locked_posts':         build_locked_posts(fetched.get('ModLockPost', [])),
        'featured_posts':       build_featured_posts(fetched.get('ModFeaturePost', [])),
        'removed_comments':     build_removed_comments(fetched.get('ModRemoveComment', [])),
        'removed_communities':  build_removed_communities(fetched.get('ModRemoveCommunity', [])),
        'banned_from_community': build_banned_from_community(fetched.get('ModBanFromCommunity', [])),
        'banned':               build_banned(fetched.get('ModBan', [])),
        'added_to_community':   build_added_to_community(fetched.get('ModAddCommunity', [])),
        'transferred_to_community': [],
        'added':                build_added(fetched.get('ModAdd', [])),
        'admin_purged_persons': [],
        'admin_purged_communities': [],
        'admin_purged_posts':   [],
        'admin_purged_comments': [],
        'hidden_communities':   [],
    }


def _empty_modlog_response():
    return {
        'removed_posts': [], 'locked_posts': [], 'featured_posts': [],
        'removed_comments': [], 'removed_communities': [], 'banned_from_community': [],
        'banned': [], 'added_to_community': [], 'transferred_to_community': [],
        'added': [], 'admin_purged_persons': [], 'admin_purged_communities': [],
        'admin_purged_posts': [], 'admin_purged_comments': [], 'hidden_communities': [],
    }
