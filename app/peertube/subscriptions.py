"""A person's subscriptions in PeerTube's API (spec 2026-11-09-peertube-login-design §3, ruling V2): a channel is a
community, so subscribing is joining it and unsubscribing leaving it, through app.shared.community with its checks
and federation. The subscriptions' videos are the joined communities' videos, as the person sees them."""
from flask import g, jsonify, request
from sqlalchemy import desc, func

from app import db
from app.constants import SRC_API
from app.models import Community, CommunityMember, Post
from app.peertube import bp, entities
from app.peertube.actors import MAX_HANDLE, find_community
from app.peertube.auth import failure, problem, signed_in
from app.peertube.videos import SORTS, listable_communities
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.viewer import Restrictions
from app.shared import community as shared_community
from app.utils import Authorised

MAX_URIS = 100


def _joined_query(user):
    return db.session.query(Community).join(CommunityMember, CommunityMember.community_id == Community.id).filter(
        CommunityMember.user_id == user.id, CommunityMember.is_banned == False)


def _named(uri: str) -> Community | None:
    """The community a subscription's `uri` names: a handle (name or name@host) or its actor URL."""
    uri = (uri or '').strip()
    if not uri or len(uri) > MAX_HANDLE:
        return None
    if not uri.startswith(('https://', 'http://')):
        return find_community(uri.removeprefix('!'))
    found = listable_communities(Community.query.filter(func.lower(Community.ap_profile_id) == uri.lower()).limit(1))
    return found[0] if found else None


def _is_member(user, community: Community) -> bool:
    return _joined_query(user).filter(Community.id == community.id).first() is not None


@bp.route('/api/v1/users/me/subscriptions')
@signed_in()
def list_subscriptions():
    start, count = entities.window()
    found = listable_communities(_joined_query(g.oauth_user).order_by(Community.title, Community.id).limit(5000))
    return jsonify({'total': len(found), 'data': [entities.channel(c) for c in found[start:start + count]]})


@bp.route('/api/v1/users/me/subscriptions', methods=['POST'])
@signed_in('write')
def subscribe():
    fields = request.get_json(silent=True)
    community = _named(fields.get('uri') if isinstance(fields, dict) else request.form.get('uri'))
    if community is None:
        return problem(404, 'not_found', 'Channel not found')
    if not _is_member(g.oauth_user, community):
        try:
            shared_community.join_community(community.id, SRC_API, Authorised(g.oauth_user))
        except Exception as error:   # a ban from it, or another refusal of the shared join
            return failure(error)
    return '', 204


@bp.route('/api/v1/users/me/subscriptions/<path:handle>', methods=['DELETE'])
@signed_in('write')
def unsubscribe(handle):
    community = _named(handle)
    if community is None:
        return problem(404, 'not_found', 'Channel not found')
    try:
        shared_community.leave_community(community.id, SRC_API, Authorised(g.oauth_user))
    except Exception as error:   # a moderator must step down first
        return failure(error)
    return '', 204


@bp.route('/api/v1/users/me/subscriptions/exist')
@signed_in()
def subscriptions_exist():
    """{uri: bool} for each asked: PeerTube's app repeats `uris`, TubeLab sends `uris[]`, either may join them by
    commas."""
    asked = [uri for value in request.args.getlist('uris') + request.args.getlist('uris[]')
             for uri in value.split(',') if uri.strip()][:MAX_URIS]
    answer = {}
    for uri in asked:
        community = _named(uri)
        answer[uri] = community is not None and _is_member(g.oauth_user, community)
    return jsonify(answer)


@bp.route('/api/v1/users/me/subscriptions/videos')
@signed_in()
def subscription_videos():
    user = g.oauth_user
    posts = post_listing(user, Restrictions(user.id), Listing(type='Subscribed', sort='', ignore_sticky=True),
                         KEYSET_PAGES).posts.filter(entities.playable_clause())
    order = SORTS.get(request.args.get('sort', '-publishedAt'), SORTS['-publishedAt'])
    return entities.paged(posts.order_by(*order, desc(Post.id)), entities.video)
