from random import randint
from time import sleep
from typing import List, Tuple

import httpx
from flask import abort, current_app
from flask_babel import _
from sqlalchemy import text

from app import db
from app.activitypub.util import actor_json_to_model, canonical_handle, webfinger_self_link_allowed
from app.community.util import search_for_community, retrieve_mods_and_backfill
from app.constants import AP_FETCH_ACCEPT
from app.feed_domains import feeds_shown_here_clause, shown_feed_communities
from app.models import BannedInstances, Feed, FeedItem, Community, _as_dict
from app.local_names import local_named, named_by_actor
from app.storefront import DomainInfo, domain_for_host, is_hosted_server
from app.utils import feed_tree, get_request


def feeds_for_form(current_feed: int, user_id: int, domain: DomainInfo) -> List[Tuple[int, str]]:
    """The parent choices for a feed of `domain`: the owner's feeds on that domain only."""
    result = [(0, _('None'))]
    feeds = feed_tree(user_id, domain)
    for feed in feeds:
        if feed['feed'].id != current_feed:
            result.append((feed['feed'].id, feed['feed'].title))
        if feed['children']:
            result.extend(feeds_for_form_children(feed['children'], current_feed, 1))
    return result


def refuse_bad_parent(form):
    """abort(400) when a submitted feed form names a parent the form does not offer: another domain's feed, a missing
    one or no integer, rather than the form shown again."""
    if form.is_submitted() and form.parent_feed_id.errors:
        abort(400)


def feeds_for_form_children(feeds, current_feed: int, depth: int) -> List[Tuple[int, str]]:
    result = []
    for feed in feeds:
        if feed['feed'].id != current_feed:
            result.append((feed['feed'].id, '--' * depth + ' ' + feed['feed'].title))
        if feed['children']:
            result.extend(feeds_for_form_children(feed['children'], current_feed, depth + 1))
    return result


def search_for_feed(address: str, allow_fetch: bool = True, retry: bool = False, *, signed_in: bool):
    """The feed `~name@server` names: one of ours on that hosted domain (never fetched), else the stored remote feed,
    else, with `allow_fetch`, the one WebFinger finds. Never one this storefront hides from its reader
    (`feed_shown_here`), who is signed in when `signed_in` says so. Every caller says (Task 7 review M5, Task 9
    review M4): the API its caller's, a page its session's, a worker True (the default domain's signed-in reader)."""
    if address.startswith('~'):
        # Exactly two parts, or the address is not one: '~a@b@c' used to raise
        # ValueError out of this unpacking, and feed_add_remote's first arm
        # accepts any '~...@...' string, so that was a 500 from the add-remote
        # box. The callers already have a not-found path for None.
        parts = address[1:].split('@')
        if len(parts) != 2:
            return None
        name, server = parts

        # One of ours is never banned (plan C4), by is_hosted_server, the rule the allowlist and ban gates share
        banned = None if is_hosted_server(server) else BannedInstances.query.filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")  # todo: create custom exception class hierarchy

        hosted = domain_for_host(server)
        if hosted:   # one of ours: that domain's own feed, or none. Never fetched (it would be a self-request)
            # nor one hidden here: an onion feed is found on the onion only (task 7)
            return Feed.query.filter(local_named(Feed, name, hosted), feeds_shown_here_clause(signed_in)).first()

        already_exists = Feed.query.filter_by(ap_id=address[1:]).first()
        if already_exists:
            return already_exists
        elif not allow_fetch:
            return None

        # Look up the profile address of the Feed using WebFinger
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                         params={'resource': f"acct:{address}"},  # include the ~ on the start to indicate we're searching for a feed
                                         retry=retry)
        except httpx.HTTPError:
            # only a Celery caller passes retry; the request thread must not sleep
            if not retry:
                return None
            sleep(randint(3, 10))
            try:
                webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                             params={'resource': f"acct:{address}"}, retry=True)
            except httpx.HTTPError:
                return None

        if webfinger_data.status_code == 200:
            webfinger_json = webfinger_data.json()
            # D1397, as search_for_community:77.
            if not isinstance(webfinger_json, dict) or not isinstance(webfinger_json.get('links'), list):
                return None
            for links in webfinger_json['links']:
                links = _as_dict(links)
                if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                    if 'href' not in links:  # a peer may advertise several, so keep walking
                        continue
                    if not webfinger_self_link_allowed(links['href'], f'~{name}', server):
                        return None
                    type = links['type'] if 'type' in links else AP_FETCH_ACCEPT
                    # retrieve the activitypub profile
                    feed_data = get_request(links['href'], headers={'Accept': type}, retry=retry)
                    if feed_data.status_code == 200:
                        feed_json = feed_data.json()
                        feed_data.close()
                        if feed_json['type'] == 'Feed':
                            handle = canonical_handle(feed_json, name, server, prefix='~')   # interop C1 item 9
                            if handle is None:
                                return None
                            name, server, feed_json = handle   # the origin's document, never an alias's copy
                            feed = actor_json_to_model(feed_json, name, server)
                            if feed:
                                initialise_new_communities(feed)
                                return feed
        return None


def actor_to_feed(actor, signed_in: bool | None = None) -> Feed | None:
    """The unbanned feed a `/f/<actor>` path segment names: a local name on its domain (a bare name is the
    storefront's, `name@host` that hosted domain's, never fetched), else the remote feed whose `ap_id` the handle is.
    The one resolver for every path that names a feed. None for a feed this storefront hides from its reader
    (`feed_shown_here`; `signed_in` as there), as for one nobody holds: an onion feed exists on the onion only."""
    return Feed.query.filter(named_by_actor(Feed, actor.strip().lower()), Feed.banned.is_(False),
                             feeds_shown_here_clause(signed_in)).first()


def feed_communities_for_edit(feed_id: int) -> str:
    return_value = []
    shown = shown_feed_communities(Community.query.filter(Community.banned == False), True)   # not one hidden here
    for community in shown.join(FeedItem, FeedItem.community_id == Community.id).filter(FeedItem.feed_id == feed_id).all():
        return_value.append(community.lemmy_link().lstrip('!'))   # named by its own home domain, whichever storefront this is
    return "\n".join(sorted(return_value))


def initialise_new_communities(feed):
    if feed.num_communities == 0:
        return

    for feed_item in feed.member_communities:
        community = db.session.get(Community, feed_item.community_id)
        if community and community.post_count == 0:
            if current_app.debug:
                retrieve_mods_and_backfill(community.id, community.ap_domain, community.name)
                break  # just get 2 posts from 1 new community when in debug
            else:
                retrieve_mods_and_backfill.delay(community.id, community.ap_domain, community.name)
