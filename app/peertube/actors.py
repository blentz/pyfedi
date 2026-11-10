"""PeerTube's accounts and channels (spec 2026-11-09-peertube-read-api-design §3.3, ruling V2): a person is an account,
every community a channel, each named `name` (this domain's) or `name@host`. A banned, deleted, private or hidden one
is not found, as a missing one is not.

Mastodon's /api/v1/accounts/<int> routes take a number; these take a name, which is never a bare number on PieFed,
so the two never answer the same path."""
from flask import jsonify
from sqlalchemy import desc, or_

from app import db
from app.local_names import named_by_actor
from app.local_only import hidden_here
from app.models import Community, CommunityMember, User
from app.peertube import bp
from app.peertube import entities
from app.peertube.auth import signed_in
from app.peertube.entities import not_found
from app.peertube.videos import listable_communities, videos
from app.reads.posts import Listing

MAX_HANDLE = 256


def find_person(handle: str) -> User | None:
    if not handle or len(handle) > MAX_HANDLE:
        return None
    person = User.query.filter(named_by_actor(User, handle.lower()), User.deleted == False,
                               User.banned == False).first()
    return person if person is not None and not hidden_here(person, entities.viewer() is not None) else None


def find_community(handle: str) -> Community | None:
    if not handle or len(handle) > MAX_HANDLE:
        return None
    found = listable_communities(Community.query.filter(named_by_actor(Community, handle.lower())).limit(1))
    return found[0] if found else None


@bp.route('/api/v1/accounts/<handle>')
@signed_in(optional=True)
def get_account(handle):
    person = find_person(handle)
    return jsonify(entities.account(person)) if person is not None else not_found()


@bp.route('/api/v1/accounts/<handle>/videos')
@signed_in(optional=True)
def account_videos(handle):
    person = find_person(handle)
    return videos(Listing(sort='', person_id=person.id, ignore_sticky=True)) if person is not None else not_found()


@bp.route('/api/v1/accounts/<handle>/video-channels')
@signed_in(optional=True)
def account_channels(handle):
    """The communities the person moderates or owns."""
    person = find_person(handle)
    if person is None:
        return not_found()
    start, count = entities.window()
    moderated = db.session.query(Community).join(CommunityMember, CommunityMember.community_id == Community.id).filter(
        CommunityMember.user_id == person.id, CommunityMember.is_banned == False,
        or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True)).order_by(
        desc(Community.total_subscriptions_count), Community.id).limit(1000)
    found = listable_communities(moderated)
    return jsonify({'total': len(found), 'data': [entities.channel(c) for c in found[start:start + count]]})


@bp.route('/api/v1/accounts/<handle>/video-playlists')
@signed_in(optional=True)
def account_playlists(handle):
    return entities.empty() if find_person(handle) is not None else not_found()


@bp.route('/api/v1/video-channels/<handle>')
@signed_in(optional=True)
def get_channel(handle):
    community = find_community(handle)
    return jsonify(entities.channel(community)) if community is not None else not_found()


@bp.route('/api/v1/video-channels/<handle>/videos')
@signed_in(optional=True)
def channel_videos(handle):
    community = find_community(handle)
    if community is None:
        return not_found()
    return videos(Listing(sort='', community_id=community.id, ignore_sticky=True))


@bp.route('/api/v1/video-channels/<handle>/video-playlists')
@signed_in(optional=True)
def channel_playlists(handle):
    return entities.empty() if find_community(handle) is not None else not_found()
