from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO
from json import JSONDecodeError
from random import randint
from typing import Union, Tuple, List
from urllib.parse import urlparse, parse_qs

import pendulum
import boto3
import httpx
import pytesseract
from PIL import Image, ImageOps
from flask import current_app, request, g, url_for, json
from flask_babel import _, force_locale, gettext
from furl import furl
from sqlalchemy import text, Integer, update
from sqlalchemy.exc import IntegrityError

from app import db, cache, celery, plugins
from app.activitypub.signature import signed_get_request, send_post_request, default_context, RsaKeys
from app.constants import *
from app.models import User, Post, Community, File, PostReply, Instance, utcnow, \
    PostVote, PostReplyVote, ActivityPubLog, Notification, Site, CommunityMember, InstanceRole, Report, Conversation, \
    Language, Tag, Poll, PollChoice, CommunityBan, CommunityJoinRequest, NotificationSubscription, \
    Licence, UserExtraField, Feed, FeedMember, FeedItem, CommunityFlair, UserFlair, Topic, Event, InstanceBan, Emoji, \
    UserFollower, PostBoost, QuoteAuthorization, parse_ap_timestamp, image_url_from, markdown_source, \
    _as_text, _as_int, _as_float, _as_dict, _as_url, property_value_fields, public_key_pem, \
    more_info_link, is_more_info_link, more_info_url_from, \
    language_from_ap, adjust_domain_post_count, actor_name_from_ap, PostReplyValidationError, post_file
from app.utils import get_request, allowlist_html, get_setting, ap_datetime, markdown_to_html, \
    sanitise_posting_warning, \
    is_image_url, domain_from_url, gibberish, ensure_directory_exists, shorten_string, fixup_url, \
    microblog_content_to_title, is_video_url, \
    notification_subscribers, communities_banned_from, html_to_text, add_to_modlog, joined_communities, \
    moderating_communities, get_task_session, is_video_hosting_site, opengraph_parse, mastodon_extra_field_link, \
    blocked_users, piefed_markdown_to_lemmy_markdown, store_files_in_s3, guess_mime_type, get_recipient_language, \
    patch_db_session, to_srgb, communities_banned_from_all_users, blocked_communities, blocked_or_banned_instances, \
    instance_community_ids, banned_instances, communities_run_by_inactive_mods, inspect_image_c2pa, \
    url_is_storable, can_create_post, can_create_post_reply, retrieve_image_hash, hash_matches_blocked_image
import app.activitypub.actor as activitypub_actor
import urllib.parse
from app.utils import site_language_id
from app.visibility import OPEN_VISIBILITIES, can_view
import app as app_pkg


def community_members(community_id):
    sql = 'SELECT COUNT(*) as c FROM "user" as u '
    sql += 'INNER JOIN community_member cm on u.id = cm.user_id '
    sql += 'WHERE u.banned is false AND u.deleted is false AND cm.is_banned is false and cm.community_id = :community_id'
    return db.session.execute(text(sql), {'community_id': community_id}).scalar()


def users_total():
    return db.session.execute(text(
        'SELECT COUNT(*) as c FROM "user" WHERE ap_id is null AND verified is true AND banned is false AND deleted is false')).scalar()


def active_half_year():
    return db.session.execute(text(
        "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '6 months' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_month():
    return db.session.execute(text(
        "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 month' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_week():
    return db.session.execute(text(
        "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 week' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def active_day():
    return db.session.execute(text(
        "SELECT COUNT(*) as c FROM \"user\" WHERE last_seen >= CURRENT_DATE - INTERVAL '1 day' AND ap_id is null AND verified is true AND banned is false AND deleted is false")).scalar()


def local_posts():
    return db.session.execute(
        text('SELECT COUNT(*) as c FROM "post" WHERE instance_id = 1 AND deleted is false')).scalar()


def local_comments():
    return db.session.execute(
        text('SELECT COUNT(*) as c FROM "post_reply" WHERE instance_id = 1 and deleted is false')).scalar()


def local_communities():
    return db.session.execute(text('SELECT COUNT(*) as c FROM "community" WHERE instance_id = 1')).scalar()


def post_to_activity(post: Post, community: Community):
    # local PieFed posts do not have a create or announce id
    create_id = post.ap_create_id if post.ap_create_id else f"{current_app.config['SERVER_URL']}/activities/create/{gibberish(15)}"
    announce_id = post.ap_announce_id if post.ap_announce_id else f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
    activity_data = {
        "actor": community.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": {
            "id": create_id,
            "actor": post.author.public_url(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "object": post_to_page(post),
            "cc": [
                community.public_url()
            ],
            "type": "Create",
            "audience": community.public_url()
        },
        "cc": [
            f"{community.public_url()}/followers"
        ],
        "type": "Announce",
        "id": announce_id
    }

    return activity_data


CONTENT_WARNING_MAX_LENGTH = 500


def content_warning_from(obj: dict):
    """The content warning in a peer's object: its `summary`, as plain text, or None.

    Mastodon and Pixelfed put the warning there on a Note. On a Page `summary` is
    a description (PieFed and Lemmy do not send one), so it counts there only
    alongside `sensitive: true`, which is how a warning is flagged outbound.
    """
    summary = obj.get('summary')
    if not isinstance(summary, str):
        return None
    if obj.get('type') != 'Note' and obj.get('sensitive') is not True:
        return None
    return shorten_string(html_to_text(summary).strip(), CONTENT_WARNING_MAX_LENGTH) or None


def post_to_page(post: Post):
    activity_data = {
        "type": "Page",
        "id": post.ap_id,
        "context": f"{current_app.config['SERVER_URL']}/post/{post.id}/context",
        "attributedTo": post.author.ap_public_url,
        "to": [
            post.community.public_url(),
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "name": post.title,
        "cc": [],
        "content": post.body_html if post.body_html else '',
        "mediaType": "text/html",
        "source": {"content": post.body if post.body else '', "mediaType": "text/markdown"},
        "attachment": [],
        "commentsEnabled": post.comments_enabled,
        "sensitive": post.nsfw or post.nsfl,
        "genAI": post.ai_generated,
        "published": ap_datetime(post.posted_at),
        "stickied": post.sticky,
        "audience": post.community.public_url(),
        "tag": post.tags_for_activitypub(),
        "replies": f'{current_app.config["SERVER_URL"]}/post/{post.id}/replies',
        "language": {
            "identifier": post.language_code(),
            "name": post.language_name()
        },
        'interactionPolicy': {
            'canQuote': {
                'automaticApproval': ['https://www.w3.org/ns/activitystreams#Public']
            }
        },
    }
    if post.language_id:
        activity_data['contentMap'] = {post.language_code(): activity_data['content']}
    if post.content_warning:
        activity_data['summary'] = post.content_warning
        activity_data['sensitive'] = True  # a peer takes `summary` as a warning only alongside this
    if post.edited_at is not None:
        activity_data["updated"] = ap_datetime(post.edited_at)
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        activity_data["attachment"] = [{"href": post.url, "type": "Link"}]
    if post.image_id is not None:
        activity_data["image"] = {"url": post.image.view_url(), "type": "Image"}
        if post.type == POST_TYPE_IMAGE:
            activity_data['attachment'] = [{'type': 'Image',
                                            'url': post.image.source_url,
                                            'name': post.image.alt_text}]
    # D1337. This read `poll.mode` off `.first()` and `ap_datetime(poll.end_poll)`
    # without asking whether either was there, and BOTH are reachable:
    #
    #   * a post typed POLL with no `poll` row at all was `AttributeError:
    #     'NoneType' object has no attribute 'mode'`;
    #   * `Poll.end_poll` is nullable and `app/shared/post.py` sets it only `if
    #     'end_poll' in poll_data and poll_data['end_poll']`, while the API schema
    #     marks `mode` and `choices` required and `end_poll` not -- so an API
    #     client can create a poll with no end time, and `ap_datetime(None)` is
    #     `AttributeError: 'NoneType' object has no attribute 'isoformat'`.
    #
    # This function builds what goes to peers, so either one meant the post never
    # federated, quietly, while looking correct locally.
    #
    # A poll this instance cannot describe is sent as the ordinary Page it can:
    # `Post.new` REFUSES a Question with no endTime (D1330), so a Question sent
    # without one is a document PieFed itself would drop the poll from.
    if post.type == POST_TYPE_POLL:
        poll = Poll.query.filter_by(post_id=post.id).first()
        if poll is not None and poll.end_poll is not None:
            activity_data['type'] = 'Question'
            del activity_data['name']
            activity_data['content'] = f"<p>{post.title}</p>{post.body_html if post.body_html else ''}"
            mode = 'oneOf' if poll.mode == 'single' else 'anyOf'
            choices = []
            for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
                choices.append({
                    "type": "Note",
                    "name": choice.choice_text,
                    "replies": {
                        "type": "Collection",
                        "totalItems": choice.num_votes
                    }
                })
            activity_data[mode] = choices
            activity_data['endTime'] = ap_datetime(poll.end_poll)
            activity_data['votersCount'] = poll.total_votes()
    elif post.type == POST_TYPE_EVENT:
        # D1338, the same shape as D1337 one branch up and reachable the same two
        # ways: a post typed EVENT with no `event` row was `AttributeError:
        # 'NoneType' object has no attribute 'start'`, and `Event.start`/`Event.end`
        # are nullable -- `app/shared/post.py` sets `end` only `if 'end' in
        # event_data and event_data['end']` -- so `ap_datetime(None)` was
        # `AttributeError: 'NoneType' object has no attribute 'isoformat'`.
        #
        # An event with no start is not an event this instance can describe, so it
        # goes out as the Page it can. An end time is optional in the document:
        # `Post.new` reads it with `.get` (D1339), so a peer running PieFed takes
        # the event without one.
        event = Event.query.filter_by(post_id=post.id).first()
        if event is not None and event.start is not None:
            activity_data['type'] = 'Event'
            activity_data['startTime'] = ap_datetime(event.start)
            if event.end is not None:
                activity_data['endTime'] = ap_datetime(event.end)
            # D306 (owner ruling): a property the event does not have is left out, not sent as a null
            optional = {'timezone': event.timezone,
                        'maximumAttendeeCapacity': event.max_attendees,
                        'participantCount': event.participant_count,
                        'onlineLink': event.online_link,
                        'joinMode': event.join_mode,
                        'externalParticipationUrl': event.external_participation_url,
                        'anonymousParticipation': event.anonymous_participation,
                        'isOnline': event.online,
                        'buyTicketsLink': event.buy_tickets_link,
                        'feeCurrency': event.event_fee_currency,
                        'feeAmount': event.event_fee_amount,
                        'location': event.location}
            activity_data.update({key: value for key, value in optional.items() if value is not None})
            if event.more_info_url:  # R223
                activity_data['attachment'].append(more_info_link(event.more_info_url))

    if post.indexable:
        activity_data['searchableBy'] = 'https://www.w3.org/ns/activitystreams#Public'
    return activity_data


def post_replies_for_ap(post_id: int) -> List[dict]:
    replies = PostReply.query.filter_by(post_id=post_id, deleted=False).filter(PostReply.visibility.in_(OPEN_VISIBILITIES)) \
        .order_by(PostReply.posted_at).limit(2000)
    return [comment_model_to_json(reply) for reply in replies]


def comment_model_to_json(reply: PostReply) -> dict:
    reply_data = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "type": "Note",
        "id": reply.ap_id,
        "context": f"{current_app.config['SERVER_URL']}/post/{reply.post.id}/context",
        "attributedTo": reply.author.public_url(),
        "inReplyTo": reply.in_reply_to(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public",
            reply.to()
        ],
        "cc": [
            reply.community.public_url(),
            reply.author.followers_url()
        ],
        'content': reply.body_html,
        'mediaType': 'text/html',
        'source': {'content': reply.body, 'mediaType': 'text/markdown'},
        'published': ap_datetime(reply.created_at),
        'distinguished': reply.distinguished,
        'audience': reply.community.public_url(),
        'language': {
            'identifier': reply.language_code(),
            'name': reply.language_name()
        },
        'flair': reply.author.community_flair(reply.community_id),
        'repliesEnabled': reply.replies_enabled,
        'answer': reply.answer,
        'interactionPolicy': {
            'canQuote': {
                'automaticApproval': ['https://www.w3.org/ns/activitystreams#Public']
            }
        },
        'tag': reply.tags_for_activitypub()
    }
    if reply.content_warning:
        reply_data['summary'] = reply.content_warning
        reply_data['sensitive'] = True
    if reply.edited_at:
        reply_data['updated'] = ap_datetime(reply.edited_at)
    if reply.deleted:
        if reply.deleted_by == reply.user_id:
            reply_data['content'] = '<p>Deleted by author</p>'
            reply_data['source']['content'] = 'Deleted by author'
        else:
            reply_data['content'] = '<p>Deleted by moderator</p>'
            reply_data['source']['content'] = 'Deleted by moderator'
    return reply_data


def banned_user_agents():
    return []  # todo: finish this function


def find_actor_or_create(actor: str, create_if_not_found=True, community_only=False, feed_only=False,
                         allow_banned=False, retry=False) -> Union[User, Community, Feed, None]:
    """Find an actor by URL or webfinger, optionally creating it if not found.

    retry lets a failed fetch sleep and try again. Only housekeeping Celery tasks
    pass it: the inbox and web requests must not sleep (D775).

    Consider using find_actor_or_create_cached() for better performance
    """
    if isinstance(actor, dict):
        actor = actor['id']

    actor_url = actor.strip()
    if not activitypub_actor.validate_remote_actor(actor_url, allow_banned=allow_banned):
        return None

    # Find the actor
    actor_obj = activitypub_actor.find_actor_by_url(actor_url, community_only, feed_only, allow_banned=allow_banned)

    if actor_obj is False:  # banned or deleted actor was found
        return None

    if actor_obj:
        # Schedule a refresh if needed
        activitypub_actor.schedule_actor_refresh(actor_obj)
        return actor_obj
    elif create_if_not_found:
        # Create the actor from remote data
        return activitypub_actor.create_actor_from_remote(actor_url, community_only, feed_only, retry=retry)
    else:
        return None


@cache.memoize(timeout=600)  # 10 minutes
def _find_actor_id_cached(actor_url: str, community_only: bool, feed_only: bool) -> Union[Tuple[int, str], None]:
    """Cache only the actor ID and type, not the full SQLAlchemy model (cache.memoize cannot do DB models).
    Returns a tuple of (id, class_name) or None if not found, to avoid Redis serialization issues with SQLAlchemy models.
    """
    actor_obj = find_actor_or_create(actor_url, create_if_not_found=False, community_only=community_only, feed_only=feed_only)
    if actor_obj:
        return (actor_obj.id, actor_obj.__class__.__name__)
    return None


def find_actor_or_create_cached(actor: str, create_if_not_found=True, community_only=False, feed_only=False) -> Union[User, Community, Feed, None]:
    """Cached wrapper for find_actor_or_create().

    This function provides significantly better performance (~24x faster) by caching
    the actor ID in Redis and then fetching the model from the database using model.get().

    Args:
        actor: Actor URL or dict with 'id' key
        create_if_not_found: If True, create the actor if not found (default: True)
        community_only: Only return Community actors
        feed_only: Only return Feed actors

    Returns:
        The actor model (User, Community, or Feed) or None if not found
    """

    if isinstance(actor, dict):
        actor = actor['id']

    actor_url = actor.strip()
    if not activitypub_actor.validate_remote_actor(actor_url):
        return None

    # Try to get cached ID and type
    result = _find_actor_id_cached(actor_url, community_only, feed_only)

    if result:
        actor_id, actor_type = result
        # Fetch fresh model from database using primary key lookup (very fast)
        actor_obj = None
        if actor_type == 'User':
            actor_obj = db.session.get(User, actor_id)
        elif actor_type == 'Community':
            actor_obj = db.session.get(Community, actor_id)
        elif actor_type == 'Feed':
            actor_obj = db.session.get(Feed, actor_id)

        # The cache holds only the id, so an actor banned or deleted since it was cached must be refused here, with
        # the same checks the uncached path applies: find_local_user for local users, validate_remote_actor otherwise
        if actor_obj:
            if actor_obj.is_local():
                if isinstance(actor_obj, User) and actor_obj.banned:
                    return None
            elif not activitypub_actor.validate_remote_actor(actor_url, actor_obj):
                return None

            # Schedule a refresh if needed
            activitypub_actor.schedule_actor_refresh(actor_obj)
            return actor_obj

    # Not in cache - fall back to the original function
    actor_obj = find_actor_or_create(actor_url, create_if_not_found=create_if_not_found, community_only=community_only, feed_only=feed_only)
    # Cache the actor for next time
    if actor_obj:
        cache.set(f"_find_actor_id_cached({actor_url},{community_only},{feed_only})",
                 (actor_obj.id, actor_obj.__class__.__name__), timeout=600)
    return actor_obj


def find_language(code: str) -> Language | None:
    existing_language = Language.query.filter(Language.code == code).first()
    if existing_language:
        return existing_language
    else:
        return None


def find_language_or_create(code: str, name: str, session=None) -> Language:
    if session:
        existing_language: Language = session.query(Language).filter(Language.code == code).first()
    else:
        existing_language = Language.query.filter(Language.code == code).first()
    if existing_language:
        return existing_language
    else:
        new_language = Language(code=code, name=name)
        if session:
            session.add(new_language)
        else:
            db.session.add(new_language)
        return new_language


def find_licence_or_create(name: str) -> Licence:
    existing_licence = Licence.query.filter(Licence.name == name.strip()).first()
    if existing_licence:
        return existing_licence
    else:
        new_licence = Licence(name=name.strip())
        db.session.add(new_licence)
        return new_licence


def find_hashtag_or_create(hashtag: str) -> Tag:
    if hashtag is None or hashtag == '':
        return None

    hashtag = hashtag.strip()
    if hashtag[0] == '#':
        hashtag = hashtag[1:]

    existing_tag = Tag.query.filter(Tag.name == hashtag.lower()).first()
    if existing_tag:
        return existing_tag
    else:
        new_tag = Tag(name=hashtag.lower(), display_as=hashtag, post_count=1)
        db.session.add(new_tag)
        return new_tag


def find_flair_or_create(flair: dict, community_id: int, session=None) -> CommunityFlair | None:
    if session is None:
        session = db.session
    if 'id' in flair:
        existing_flair = session.query(CommunityFlair).filter(CommunityFlair.ap_id == flair['id']).first()
    else:
        existing_flair = None

    if existing_flair is None:
        if 'preferredUsername' in flair:
            lookup_name = flair['preferredUsername'].strip()
        elif 'display_name' in flair:
            lookup_name = flair['display_name'].strip()
        else:
            lookup_name = None
        if lookup_name is not None:
            existing_flair = session.query(CommunityFlair).filter(CommunityFlair.flair == lookup_name,
                                                                  CommunityFlair.community_id == community_id).first()
            if existing_flair is None:
                # Under autoflush=False a row an earlier call created in this session
                # is still pending and invisible to the query above, so one peer list
                # naming a flair twice created it twice (D27). Search the pending rows too.
                existing_flair = next((pending for pending in session.new
                                       if isinstance(pending, CommunityFlair)
                                       and pending.community_id == community_id
                                       and pending.flair == lookup_name), None)

    if existing_flair:
        # Update flair properties
        if "text_color" in flair:
            existing_flair.text_color = flair['text_color']
        elif "textColor" in flair:
            existing_flair.text_color = flair["textColor"]
        
        if "background_color" in flair:
            existing_flair.background_color = flair['background_color']
        elif "backgroundColor" in flair:
            existing_flair.background_color = flair["backgroundColor"]
        
        if "blur_images" in flair:
            existing_flair.blur_images = flair['blur_images'] if 'blur_images' in flair else False
        elif "blurImages" in flair:
            existing_flair.blur_images = flair["blurImages"]
        
        if "display_name" in flair:
            existing_flair.flair = flair["display_name"].strip()
        elif "preferredUsername" in flair:
            existing_flair.flair = flair['preferredUsername'].strip()

        if not existing_flair.ap_id:
            if 'id' in flair and flair['id']:
                existing_flair.ap_id = flair['id']
            elif existing_flair.id is not None:  # a pending row has no id to derive from yet
                # The peer named no ap_id for this entry: either the key is absent
                # (legacy 'lemmy:tagsForPosts' entries never carry one) or it is empty.
                # Skip the peer's id the way every other optional key here is skipped,
                # and derive a local one instead. Reading flair['id'] unconditionally
                # used to raise KeyError out of refresh_community_profile_task AFTER
                # that task had committed the community's refreshed profile.
                current_app.logger.info(
                    f"find_flair_or_create: skipping the ap_id backfill for flair "
                    f"{existing_flair.flair!r} on community {community_id} -- the peer's "
                    f"entry supplies no usable 'id'; deriving a local ap_id instead")
                existing_flair.ap_id = existing_flair.get_ap_id()

        return existing_flair
    else:
        flair_text = text_color = background_color = ''
        blur_images = False
        if "text_color" in flair:
            text_color = flair['text_color']
        elif "textColor" in flair:
            text_color = flair["textColor"]

        if "background_color" in flair:
            background_color = flair['background_color']
        elif "backgroundColor" in flair:
            background_color = flair["backgroundColor"]

        if "blur_images" in flair:
            blur_images = flair['blur_images'] if 'blur_images' in flair else False
        elif "blurImages" in flair:
            blur_images = flair["blurImages"]

        if "display_name" in flair:
            flair_text = flair["display_name"]
        elif "preferredUsername" in flair:
            flair_text = flair['preferredUsername']
        
        new_ap_id = flair["id"] if "id" in flair else None

        if flair_text:
            new_flair = CommunityFlair(flair=flair_text.strip(), community_id=community_id,
                                       text_color=text_color, background_color=background_color,
                                       blur_images=blur_images,
                                       ap_id=new_ap_id)
            session.add(new_flair)
            return new_flair
        else:
            return None


def find_flair(flair: str, community_id: int, session=None) -> CommunityFlair | None:
    if session is None:
        session = db.session
    return session.query(CommunityFlair).\
        filter(CommunityFlair.flair == flair, CommunityFlair.community_id == community_id).first()


def update_community_flair_from_tags(community: Community, flair_tags: list, session=None):
    """Update community flair from activity_json tags, preserving existing post associations."""
    if session is None:
        session = db.session
    
    # Track which flair should be kept
    updated_flair = []
    processed_ap_ids = set()
    processed_names = set()
    
    # Update existing flair or create new ones
    for flair in flair_tags:
        updated_flair_obj = find_flair_or_create(flair, community.id, session)
        if updated_flair_obj is None:
            return
        updated_flair.append(updated_flair_obj)
        
        # Track which flair should be kept.
        if updated_flair_obj.ap_id:
            processed_ap_ids.add(updated_flair_obj.ap_id)
        if updated_flair_obj.flair:
            processed_names.add(updated_flair_obj.flair)
    
    # Remove flair that's no longer in the activity_json (check processed_ap_ids and processed_names)
    remove_outdated_community_flair(community, processed_ap_ids, processed_names, session)


def remove_outdated_community_flair(community: Community, keep_ap_ids: set, keep_names: set, session=None):
    """Remove community flair that's no longer present in the remote activity."""
    if session is None:
        session = db.session
    
    flair_to_remove = []
    for existing_flair in community.flair:
        should_remove = False
        if existing_flair.ap_id and existing_flair.ap_id not in keep_ap_ids:
            should_remove = True
        elif not existing_flair.ap_id and existing_flair.flair not in keep_names:
            should_remove = True
        
        if should_remove:
            flair_to_remove.append(existing_flair)
    
    # Remove outdated flair
    for flair in flair_to_remove:
        community.flair.remove(flair)
        session.delete(flair)


def host_of(url_string: str) -> str:
    """The lowercased host of a URL, or '' when it has none.

    RFC 3986 section 3.2: the authority may carry userinfo and a port, neither
    of which identifies the host. urlparse's `hostname` strips both and
    lowercases what is left; `netloc` does not, which is the defect this
    closes at every call site.

    Every string reaching this function is chosen by a remote peer, and
    urlparse raises ValueError on a netloc it refuses -- an unbalanced IPv6
    bracket, two '::' runs, a host failing its NFKC confusability check. We
    degrade to '' rather than propagate, matching extract_domain_and_actor.

    '' rather than None is deliberate, but it does not by itself stop two
    failed parses comparing equal -- '' == '' just as None == None. The
    return value only makes the obligation checkable; discharging it is the
    caller's job, and each caller does so differently:

    - ensure_domains_match refuses an empty side explicitly, because both of
      its operands are peer-supplied and either can fail to parse.
    - verify_object_from_source returns early when the object URI has no
      host, which leaves that operand provably non-empty at both later
      comparisons, so an empty other side can only compare unequal.
    - actor_json_to_model's server gate discharges it directly, since
      2026-08-29: it refuses an id with no host rather than comparing it.
      It used to rely instead on `server` being derived locally and
      non-empty at every call site, and that reliance was misplaced --
      create_actor_from_remote takes `server` from extract_domain_and_actor,
      which returns ('', '') on exactly the ValueError this function
      swallows, so an unparseable id compared equal to an unparseable server
      and the gate accepted. Registered as D32 and fixed there; the caller
      obligation is no longer load-bearing for that gate.
    - find_cross_host_actors, in app/cli.py, discharges it by DIRECTION
      rather than by a check: only one of its two operands comes from here,
      and the other is the row's own ap_domain, lowercased. An ap_profile_id
      this function refuses becomes '' and so compares UNEQUAL to any
      non-empty ap_domain -- the row is reported, which is the wanted
      outcome. The pair goes unreported only when ap_domain is empty too,
      and then the audit under-reports rather than over-reports. For a
      read-only report that is the safe direction: the failure mode is a
      missed row, never a truthful peer named as a suspect.
    """
    try:
        return urlparse(url_string).hostname or ''
    except ValueError:
        return ''


def on_owner_host(entry, owner_url: str) -> bool:
    """Whether a collection entry (a URL, or an object carrying one as 'id') is on the collection owner's host.

    Refresh tasks create unseen actors only for entries that pass this, so a peer's collection cannot make us fetch
    actors from other hosts. An owner whose host does not parse matches nothing, rather than '' matching ''."""
    entry_id = entry.get('id') if isinstance(entry, dict) else entry
    owner_host = host_of(owner_url)
    return bool(owner_host) and isinstance(entry_id, str) and host_of(entry_id) == owner_host


def extract_domain_and_actor(url_string: str):
    # Parse the URL
    if url_string.endswith('/'):  # WordPress
        url_string = url_string[:-1]
    try:
        parsed_url = urlparse(url_string)
    except ValueError:
        # The actor id is chosen by the REMOTE peer (validate_remote_actor on
        # the inbound federation path), so a broken or hostile one can send a
        # netloc urlparse refuses: an unbalanced IPv6 bracket, an address with
        # two '::' runs, or a host that fails its NFKC confusability check.
        # ('', '') is what urlparse already yields for a string with no
        # authority and no path, so nothing new is invented; every caller
        # degrades to "actor not found" from there.
        return '', ''

    # Extract the server domain name
    server_domain = parsed_url.netloc

    # Extract the part of the string after the last '/' character
    actor = parsed_url.path.split('/')[-1]

    return server_domain, actor


# The refresh tasks act on at most this many entries of a peer's collection, since the peer chooses its length (D225)
REFRESH_COLLECTION_LIMIT = 50


def refresh_user_profile(user_id, activity_json=None):
    if current_app.debug:
        refresh_user_profile_task(user_id, activity_json)
    else:
        refresh_user_profile_task.apply_async(args=(user_id, activity_json), countdown=randint(1, 10))


@celery.task
def refresh_user_profile_task(user_id, activity_json=None):
    session = get_task_session()
    try:
        with patch_db_session(session):
            user: User = session.get(User, user_id)
            if user and user.instance_id and user.instance.online():
                if not activity_json:
                    try:
                        actor_data = get_request(user.ap_public_url, headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:  # get_request has already retried once (D224)
                        return
                    except Exception:  # not bare: a worker shutdown must propagate (D220)
                        try:
                            site = session.get(Site, 1)
                            actor_data = signed_get_request(user.ap_public_url, site.private_key,
                                                            f"{current_app.config['SERVER_URL']}/actor#main-key")
                        except Exception:
                            return
                    if actor_data.status_code == 200:
                        try:
                            activity_json = actor_data.json()
                            actor_data.close()
                        except JSONDecodeError:
                            user.instance.failures += 1
                            session.commit()
                            return

                if activity_json:
                    # update indexible state on their posts, if necessary
                    new_indexable = activity_json['indexable'] if 'indexable' in activity_json else True
                    if new_indexable != user.indexable:
                        session.execute(text('UPDATE "post" set indexable = :indexable WHERE user_id = :user_id'),
                                        {'user_id': user.id,
                                         'indexable': new_indexable})

                    # fix ap_id for WordPress actors
                    if user.ap_id.startswith('@'):
                        server, address = extract_domain_and_actor(user.ap_profile_id)
                        user.ap_id = f"{address.lower()}@{server.lower()}"

                    # D1372. `activity_json['preferredUsername'].strip()`. Absent
                    # was KeyError and a non-string was AttributeError, and this
                    # task re-raises, so an actor whose document carried
                    # `preferredUsername: null` could never be refreshed again --
                    # their avatar, bio, indexable flag and rotated key all stopped
                    # being picked up. A peer that stops publishing a usable name
                    # has not renamed itself to nothing, so the name this instance
                    # already holds stays (public_key_pem's reasoning, D1354).
                    refreshed_name = actor_name_from_ap(activity_json)
                    if refreshed_name:
                        user.user_name = refreshed_name
                    if 'name' in activity_json:
                        user.title = actor_name_from_ap(activity_json, 'name',
                                                        limit=256) or ''
                    if 'summary' in activity_json:
                        # D1355. `summary` is whatever the peer sent: a number, a
                        # list or an object was `AttributeError: ... has no
                        # attribute 'startswith'` out of this task, and the actor
                        # was then unrefreshable.
                        about_html = _as_text(activity_json['summary'])
                        if about_html is not None and not about_html.startswith('<'):  # PeerTube
                            about_html = '<p>' + about_html + '</p>'
                        user.about_html = allowlist_html(about_html)
                    else:
                        user.about_html = ''
                    source_markdown = markdown_source(activity_json)  # D1346
                    if source_markdown is not None:
                        user.about = source_markdown
                        user.about_html = markdown_to_html(user.about)  # prefer Markdown if provided, overwrite version obtained from HTML
                    else:
                        user.about = html_to_text(user.about_html)
                    if 'attachment' in activity_json:
                        # D1354. One reading of these entries, which skips an entry
                        # it cannot use rather than raising out of this task and
                        # leaving the actor unrefreshable.
                        user.extra_fields = [
                            UserExtraField(label=label, text=text)
                            for label, text in property_value_fields(
                                activity_json['attachment'])]
                    if 'type' in activity_json:
                        user.bot = True if activity_json['type'] == 'Service' else False
                    user.ap_fetched_at = utcnow()
                    # D1354. A document with no readable `publicKey` leaves the
                    # key this instance already holds: an actor that stops
                    # publishing one has not rotated to nothing, and storing the
                    # string 'None' -- which is what `{'publicKeyPem': None}` used
                    # to do -- breaks every signature check against them.
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        user.public_key = refreshed_pem
                    user.accept_private_messages = activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
                    user.indexable = new_indexable

                    if user.title and user.title.strip().lower() == '[deleted]':
                        user.title = ''

                    avatar_changed = cover_changed = False
                    if 'icon' in activity_json and activity_json['icon'] is not None:
                        icon_entry = image_url_from(activity_json['icon'], prefer_last=True)
                        if icon_entry:
                            if user.avatar_id and icon_entry != user.avatar.source_url:
                                user.avatar.delete_from_disk()
                            if not user.avatar_id or (user.avatar_id and icon_entry != user.avatar.source_url):
                                avatar = File(source_url=icon_entry)
                                user.avatar = avatar
                                session.add(avatar)
                                avatar_changed = True
                    if 'image' in activity_json and activity_json['image'] is not None:
                        cover_entry = image_url_from(activity_json['image'])
                        if cover_entry:
                            if user.cover_id and cover_entry != user.cover.source_url:
                                user.cover.delete_from_disk()
                            if not user.cover_id or (user.cover_id and cover_entry != user.cover.source_url):
                                cover = File(source_url=cover_entry)
                                user.cover = cover
                                session.add(cover)
                                cover_changed = True

                    session.commit()
                    if user.avatar_id and avatar_changed and get_setting('cache_remote_images_locally', True):
                        make_image_sizes(user.avatar_id, 40, 250, 'users')
                    if user.cover_id and cover_changed and get_setting('cache_remote_images_locally', True):
                        make_image_sizes(user.cover_id, 700, 1600, 'users')
                        cache.delete_memoized(User.cover_image, user)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def posting_warning_from_ap(activity_json):
    """The posting warning a peer publishes, sanitised, or None.

    D1377. `activity_json['postingWarning']` went into `Community.posting_warning`
    verbatim at both the refresh and the creation site, and
    `app/templates/post/post.html:91` renders it

        {{ post.community.posting_warning|safe }}

    on every post page in that community. `|safe` is deliberate -- a local
    moderator may format the warning -- but it means a REMOTE community's warning
    was peer-supplied HTML rendered unescaped. Measured: a Group document
    publishing `<img src=x onerror=alert(1)><script>alert(2)</script>` had exactly
    that stored, from both the refresh and the creation path.

    `allowlist_html` is what every other peer-sourced HTML field in this module
    goes through, so the affordance survives and the script does not. The value is
    also typed and bounded: `{}` was `ProgrammingError: can't adapt type 'dict'`
    and a 700-character warning was `DataError` on a String(512), either of which
    aborts the refresh task and leaves the community unrefreshable for ever
    (D1372's family). Sanitised before the final cut, and cut again after, because
    escaping can lengthen the string; a tag the cut splits is dropped by the
    browser, and nothing that survives the allowlist can execute.
    """
    warning = _as_text(activity_json.get('postingWarning'), 512)
    if warning is None:
        return None
    return sanitise_posting_warning(warning) or None


def refresh_community_profile(community_id, activity_json=None):
    if current_app.debug:
        refresh_community_profile_task(community_id, activity_json)
    else:
        refresh_community_profile_task.apply_async(args=(community_id, activity_json), countdown=randint(1, 10))


@celery.task
def refresh_community_profile_task(community_id, activity_json=None):
    session = get_task_session()
    try:
        with patch_db_session(session):
            community: Community = session.get(Community, community_id)
            if community and community.instance_id and community.instance.online():
                if not activity_json:
                    try:
                        actor_data = get_request(community.ap_public_url, headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:  # get_request has already retried once (D224)
                        return
                    except Exception:  # a peer that requires signed fetches, as for users (D221)
                        try:
                            site = session.get(Site, 1)
                            actor_data = signed_get_request(community.ap_public_url, site.private_key,
                                                            f"{current_app.config['SERVER_URL']}/actor#main-key")
                        except Exception:
                            return
                    if actor_data.status_code == 200:
                        try:
                            activity_json = actor_data.json()
                        except JSONDecodeError:
                            community.instance.failures += 1
                            session.commit()
                            return
                        actor_data.close()

                if activity_json:
                    if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy and mbin
                        mods_url = activity_json['attributedTo']
                    elif 'moderators' in activity_json and isinstance(activity_json['moderators'], str):  # kbin
                        mods_url = activity_json['moderators']
                    else:
                        mods_url = None

                    community.nsfw = activity_json['sensitive'] if 'sensitive' in activity_json else False
                    if 'nsfl' in activity_json and activity_json['nsfl']:
                        community.nsfl = activity_json['nsfl']
                    # D1374. `activity_json['name'].strip()`. Absent was KeyError,
                    # a non-string was AttributeError and a value wider than
                    # String(256) was DataError -- and this task re-raises after
                    # rolling back, so any of them left the community stale for
                    # ever: its name, icon, description, moderator list AND key all
                    # stopped being picked up. D1372's finding at the two refresh
                    # sites it did not reach. A peer that stops publishing a usable
                    # display name has not renamed itself to nothing, so the title
                    # this instance holds stays.
                    refreshed_title = actor_name_from_ap(activity_json, 'name', limit=256)
                    if refreshed_title:
                        community.title = refreshed_title
                    community.posting_warning = posting_warning_from_ap(activity_json)  # D1377
                    community.restricted_to_mods = activity_json['postingRestrictedToMods'] if 'postingRestrictedToMods' in activity_json else False
                    community.new_mods_wanted = activity_json['newModsWanted'] if 'newModsWanted' in activity_json else False
                    community.private_mods = activity_json['privateMods'] if 'privateMods' in activity_json else False
                    community.question_answer = activity_json['questionAnswer'] if 'questionAnswer' in activity_json else False
                    # D1374. The guard read `'default_post_type'` and the value
                    # read `'defaultPostType'`: two different keys, so the guard
                    # could never be true for the key being fetched. This instance
                    # PUBLISHES `defaultPostType` (app/activitypub/routes.py:546),
                    # so between two PieFed instances the guard was always false and
                    # every refresh silently reset a remote community's setting to
                    # 'link' -- while a peer sending only the snake_case spelling
                    # was `KeyError: 'defaultPostType'` and aborted the task.
                    # `_as_text` carries the String(15) width with it; the
                    # else-'link' arm is unchanged, and matches the five settings
                    # around it that also take their default on a refresh.
                    community.default_post_type = _as_text(
                        activity_json.get('defaultPostType'), 15) or 'link'
                    community.ap_moderators_url = mods_url
                    if 'followers' in activity_json:
                        community.ap_followers_url = activity_json['followers']
                    if 'featured' in activity_json:
                        community.ap_featured_url = activity_json['featured']
                    community.ap_fetched_at = utcnow()
                    # D1355, D1354's twin: a community that cannot be refreshed keeps
                    # its stale name, icon, description, moderator list AND key.
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        community.public_key = refreshed_pem

                    if 'postUrlType' in activity_json and activity_json['postUrlType']:
                        community.post_url_type = activity_json['postUrlType']

                    # D1355. Either key is whatever the peer sent, and a value that
                    # is not a string was `AttributeError: ... has no attribute
                    # 'startswith'` below -- out of this task, leaving the profile
                    # stale for ever. The first READABLE of the two is used, so an
                    # unusable `summary` falls back to `content` rather than
                    # shadowing it.
                    description_html = _as_text(activity_json.get('summary')) or \
                        _as_text(activity_json.get('content')) or ''

                    if description_html is not None and description_html != '':
                        if not description_html.startswith('<'):  # PeerTube
                            description_html = '<p>' + description_html + '</p>'
                        community.description_html = allowlist_html(description_html)
                        source_markdown = markdown_source(activity_json)  # D1346
                        if source_markdown is not None:
                            community.description = source_markdown
                            community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
                        else:
                            community.description = html_to_text(community.description_html)
                   
                    if 'theme' in activity_json and activity_json['theme']:
                        community.theme = activity_json['theme']
                    icon_changed = cover_changed = False
                    if 'icon' in activity_json:
                        icon_entry = image_url_from(activity_json['icon'], prefer_last=True)
                        if icon_entry:
                            if community.icon_id and icon_entry != community.icon.source_url:
                                community.icon.delete_from_disk()
                            if not community.icon_id or (community.icon_id and icon_entry != community.icon.source_url):
                                icon = File(source_url=icon_entry)
                                community.icon = icon
                                session.add(icon)
                                icon_changed = True
                    if 'image' in activity_json:
                        image_entry = image_url_from(activity_json['image'])
                        if image_entry:
                            if community.image_id and image_entry != community.image.source_url:
                                community.image.delete_from_disk()
                            if not community.image_id or (community.image_id and image_entry != community.image.source_url):
                                image = File(source_url=image_entry)
                                community.image = image
                                session.add(image)
                                cover_changed = True
                    if 'language' in activity_json and isinstance(activity_json['language'], list) and not community.ignore_remote_language:
                        for ap_language in activity_json['language']:
                            # D1355. An entry this instance cannot read is skipped
                            # rather than losing the whole refresh.
                            language = language_from_ap(ap_language)
                            if language is None:
                                continue
                            new_language = find_language_or_create(*language, session)
                            if new_language not in community.languages:
                                community.languages.append(new_language)
                    if 'genAI' in activity_json and not community.ignore_remote_gen_ai:
                        community.ai_generated = activity_json['genAI']
                    instance = session.get(Instance, community.instance_id)
                    if instance and instance.software == 'peertube':
                        community.restricted_to_mods = True
                    session.commit()

                    if 'lemmy:tagsForPosts' in activity_json and isinstance(activity_json['lemmy:tagsForPosts'], list) and "tag" not in activity_json:
                        if len(community.flair) == 0:  # for now, all we do is populate community flair if there is not yet any. simpler.
                            for flair in activity_json['lemmy:tagsForPosts']:
                                if not isinstance(flair, dict) or 'display_name' not in flair:
                                    # An entry that is not an object, or an object with no
                                    # 'display_name', has nothing to name the flair by: reading
                                    # flair['display_name'] used to raise TypeError and KeyError
                                    # respectively, AFTER the session.commit() above had written
                                    # the community's refreshed profile. This function's
                                    # `except Exception: session.rollback(); raise` cannot undo a
                                    # commit, so that left a refreshed community, no flair, an
                                    # exception at the caller and -- because the raise skips
                                    # log_incoming_ap -- nothing recording the half-ingest. Skip
                                    # the entry the way the three optional keys below it are
                                    # skipped, and say so, rather than dropping it silently.
                                    current_app.logger.warning(
                                        f"refresh_community_profile_task: skipping the "
                                        f"'lemmy:tagsForPosts' entry {flair!r} of "
                                        f"{community.ap_profile_id} -- it is not an object "
                                        f"carrying a 'display_name'")
                                    continue
                                flair_dict = {'display_name': flair['display_name']}
                                if 'text_color' in flair:
                                    flair_dict['text_color'] = flair['text_color']
                                if 'background_color' in flair:
                                    flair_dict['background_color'] = flair['background_color']
                                if 'blur_images' in flair:
                                    flair_dict['blur_images'] = flair['blur_images']
                                new_flair = find_flair_or_create(flair_dict, community.id, session)
                                if new_flair:
                                    community.flair.append(new_flair)
                            session.commit()
                    
                    if "tag" in activity_json and isinstance(activity_json["tag"], list):
                        update_community_flair_from_tags(community, activity_json["tag"], session)
                        session.commit()

                    if community.icon_id and icon_changed:
                        make_image_sizes(community.icon_id, 60, 250, 'communities')
                    if community.image_id and cover_changed:
                        make_image_sizes(community.image_id, 700, 1600, 'communities')

                    if community.ap_moderators_url:
                        mods_request = get_request(community.ap_moderators_url,
                                                   headers={'Accept': 'application/activity+json'})
                        if mods_request.status_code == 200:
                            mods_data = mods_request.json()
                            mods_request.close()
                            # isinstance, not `in`: a string value iterated its characters (D234)
                            if mods_data and 'type' in mods_data and mods_data['type'] == 'OrderedCollection' and isinstance(mods_data.get('orderedItems'), list):
                                for actor in mods_data['orderedItems'][:REFRESH_COLLECTION_LIMIT]:
                                    if not isinstance(actor.get('id') if isinstance(actor, dict) else actor, str):
                                        continue  # a malformed entry is skipped, not fatal to the rest (D219)
                                    user = find_actor_or_create(actor, create_if_not_found=on_owner_host(actor, community.ap_profile_id),
                                                                retry=True)
                                    if user:
                                        existing_membership = session.query(CommunityMember).\
                                            filter_by(community_id=community.id, user_id=user.id).first()
                                        if existing_membership:
                                            existing_membership.is_moderator = True
                                            session.commit()
                                        else:
                                            new_membership = CommunityMember(community_id=community.id, user_id=user.id,
                                                                             is_moderator=True)
                                            session.add(new_membership)
                                            session.commit()

                                # Remove people who are no longer mods
                                for member in session.query(CommunityMember).filter_by(community_id=community.id, is_moderator=True).all():
                                    member_user = session.get(User, member.user_id)
                                    is_mod = False
                                    for actor in mods_data['orderedItems']:
                                        if isinstance(actor, dict):
                                            actor = actor.get('id')
                                        if isinstance(actor, str) and actor.lower() == member_user.profile_id().lower():  # D219
                                            is_mod = True
                                            break
                                    if not is_mod:
                                        session.query(CommunityMember).filter_by(community_id=community.id,
                                                                                 user_id=member_user.id,
                                                                                 is_moderator=True).delete()
                                        session.commit()

                    if community.ap_followers_url:
                        followers_request = get_request(community.ap_followers_url, headers={'Accept': 'application/activity+json'})
                        if followers_request.status_code == 200:
                            followers_data = followers_request.json()
                            followers_request.close()
                            if followers_data and 'type' in followers_data and followers_data['type'] == 'Collection' and 'totalItems' in followers_data:
                                community.total_subscriptions_count = followers_data['totalItems']
                                session.commit()

                    if community.ap_featured_url:
                        featured_request = get_request(community.ap_featured_url, headers={'Accept': 'application/activity+json'})
                        if featured_request.status_code == 200:
                            featured_data = featured_request.json()
                            featured_request.close()
                            if featured_data and 'type' in featured_data and featured_data['type'] == 'OrderedCollection' and isinstance(featured_data.get('orderedItems'), list):  # D234
                                session.execute(text('UPDATE post SET sticky = false WHERE community_id = :community_id AND sticky = true'),
                                                {'community_id': community.id})
                                session.commit()
                                for item in featured_data['orderedItems'][:REFRESH_COLLECTION_LIMIT]:
                                    # a malformed entry is skipped, so it cannot leave every sticky cleared (D219)
                                    if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                                        continue
                                    post = Post.get_by_ap_id(item['id'])
                                    if post:
                                        post.sticky = True
                                        session.commit()

                    community.un_moderated = community.id in communities_run_by_inactive_mods()
                    session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def refresh_feed_profile(feed_id, activity_json=None):
    if current_app.debug:
        refresh_feed_profile_task(feed_id, activity_json)
    else:
        refresh_feed_profile_task.apply_async(args=(feed_id, activity_json), countdown=randint(1, 10))


@celery.task
def refresh_feed_profile_task(feed_id, activity_json=None):
    session = get_task_session()
    try:
        with patch_db_session(session):
            feed: Feed = session.get(Feed, feed_id)
            if feed and feed.instance_id and feed.instance.online() and not feed.is_local():
                if not activity_json:
                    try:
                        actor_data = get_request(feed.ap_public_url, headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:  # get_request has already retried once (D224)
                        return
                    except Exception:  # a peer that requires signed fetches, as for users (D221)
                        try:
                            site = session.get(Site, 1)
                            actor_data = signed_get_request(feed.ap_public_url, site.private_key,
                                                            f"{current_app.config['SERVER_URL']}/actor#main-key")
                        except Exception:
                            return
                    if actor_data.status_code == 200:
                        try:
                            activity_json = actor_data.json()
                        except JSONDecodeError:
                            feed.instance.failures += 1
                            session.commit()
                            return
                        actor_data.close()

                if activity_json:
                    if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy, mbin, and our feeds
                        owners_url = activity_json['attributedTo']
                    elif 'moderators' in activity_json and isinstance(activity_json['moderators'], str):  # kbin, and our feeds
                        owners_url = activity_json['moderators']
                    else:
                        owners_url = None

                    feed.nsfw = activity_json['sensitive'] if 'sensitive' in activity_json else False
                    if 'nsfl' in activity_json and activity_json['nsfl']:
                        feed.nsfl = activity_json['nsfl']
                    # D1374, as for the community refresh above.
                    refreshed_title = actor_name_from_ap(activity_json, 'name', limit=256)
                    if refreshed_title:
                        feed.title = refreshed_title
                    feed.ap_moderators_url = owners_url
                    feed.ap_fetched_at = utcnow()
                    refreshed_pem = public_key_pem(activity_json)  # D1355
                    if refreshed_pem:
                        feed.public_key = refreshed_pem

                    description_html = ''
                    # D1355. Either key is whatever the peer sent, and a value that
                    # is not a string was `AttributeError: ... has no attribute
                    # 'startswith'` below -- out of this task, leaving the profile
                    # stale for ever. The first READABLE of the two is used, so an
                    # unusable `summary` falls back to `content` rather than
                    # shadowing it.
                    description_html = _as_text(activity_json.get('summary')) or \
                        _as_text(activity_json.get('content')) or ''

                    if description_html is not None and description_html != '':
                        if not description_html.startswith('<'):  # PeerTube
                            description_html = '<p>' + description_html + '</p>'
                        feed.description_html = allowlist_html(description_html)
                        source_markdown = markdown_source(activity_json)  # D1346
                        if source_markdown is not None:
                            feed.description = source_markdown
                            feed.description_html = markdown_to_html(feed.description)          # prefer Markdown if provided, overwrite version obtained from HTML
                        else:
                            feed.description = html_to_text(feed.description_html)

                    icon_changed = cover_changed = False
                    if 'icon' in activity_json:
                        icon_entry = image_url_from(activity_json['icon'], prefer_last=True)
                        if icon_entry:
                            if feed.icon_id and icon_entry != feed.icon.source_url:
                                feed.icon.delete_from_disk()
                            if not feed.icon_id or (feed.icon_id and icon_entry != feed.icon.source_url):
                                icon = File(source_url=icon_entry)
                                feed.icon = icon
                                session.add(icon)
                                icon_changed = True
                    if 'image' in activity_json:
                        image_entry = image_url_from(activity_json['image'])
                        if image_entry:
                            if feed.image_id and image_entry != feed.image.source_url:
                                feed.image.delete_from_disk()
                            if not feed.image_id or (feed.image_id and image_entry != feed.image.source_url):
                                image = File(source_url=image_entry)
                                feed.image = image
                                session.add(image)
                                cover_changed = True
                    session.commit()

                    if feed.icon_id and icon_changed:
                        make_image_sizes(feed.icon_id, 60, 250, 'feeds')
                    if feed.image_id and cover_changed:
                        make_image_sizes(feed.image_id, 700, 1600, 'feeds')

                    if feed.ap_moderators_url:
                        owners_request = get_request(feed.ap_moderators_url,
                                                     headers={'Accept': 'application/activity+json'})
                        if owners_request.status_code == 200:
                            owners_data = owners_request.json()
                            owners_request.close()
                            if owners_data and 'type' in owners_data and owners_data['type'] == 'OrderedCollection' and isinstance(owners_data.get('orderedItems'), list):  # D234
                                for actor in owners_data['orderedItems'][:REFRESH_COLLECTION_LIMIT]:
                                    if not isinstance(actor.get('id') if isinstance(actor, dict) else actor, str):
                                        continue  # a malformed entry is skipped, not fatal to the rest (D219)
                                    user = find_actor_or_create(actor, create_if_not_found=on_owner_host(actor, feed.ap_profile_id),
                                                                retry=True)
                                    if user:
                                        existing_membership = session.query(FeedMember).filter_by(feed_id=feed.id,
                                                                                         user_id=user.id).first()
                                        if existing_membership:
                                            existing_membership.is_owner = True
                                            session.commit()
                                        else:
                                            new_membership = FeedMember(feed_id=feed.id, user_id=user.id,
                                                                        is_owner=True)
                                            session.add(new_membership)
                                            session.commit()

                                # Remove people who are no longer mods
                                # this should not get triggered as feeds just have the one owner
                                # right now, but that may change later so this is here for 
                                # future proofing
                                for member in session.query(FeedMember).filter_by(feed_id=feed.id, is_owner=True).all():
                                    member_user = session.get(User, member.user_id)
                                    is_owner = False
                                    for actor in owners_data['orderedItems']:
                                        if isinstance(actor, dict):
                                            actor = actor.get('id')
                                        if isinstance(actor, str) and actor.lower() == member_user.profile_id().lower():  # D219
                                            is_owner = True
                                            break
                                    if not is_owner:
                                        session.query(FeedMember).filter_by(feed_id=feed.id,
                                                                            user_id=member_user.id,
                                                                            is_owner=True).delete()
                                        session.commit()

                    # also make sure we have all the feeditems from the /following collection
                    if feed.ap_following_url:
                        res = get_request(feed.ap_following_url,
                                          headers={'Accept': 'application/activity+json'})
                        if res.status_code == 200:
                            try:
                                following_collection = res.json()
                            except JSONDecodeError:
                                res.close()
                                return
                            res.close()

                            # for each of those get the communities and make feeditems
                            if isinstance(following_collection, dict) and following_collection.get('type') == 'Collection' and isinstance(following_collection.get('items'), list):  # D234, D228
                                for fci in following_collection['items'][:REFRESH_COLLECTION_LIMIT]:
                                    community_ap_id = fci
                                    community = find_actor_or_create(community_ap_id, community_only=True, retry=True,
                                                                     create_if_not_found=on_owner_host(community_ap_id, feed.ap_profile_id))
                                    if community and isinstance(community, Community):
                                        feed_item = FeedItem(feed_id=feed.id, community_id=community.id)
                                        session.add(feed_item)
                                        session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def actor_json_to_model(activity_json, address, server):
    if 'type' not in activity_json:  # some Akkoma instances return an empty actor?! e.g. https://donotsta.re/users/april
        return None
    # `server` is a locally derived authority string that may carry a port, so
    # both sides go through host_of: comparing a host against an authority is
    # the same class of mistake as the substring test this replaces. The '//'
    # prefix is what makes urlparse read `server` as an authority -- without it
    # the whole string parses as a path and the host comes back ''.
    #
    # `not id_host` refuses an id with no host rather than comparing it.
    # host_of degrades a string urlparse rejects to '', and '' == '' is True,
    # so before 2026-08-29 an id urlparse refused compared EQUAL to a server
    # urlparse also refused and the gate accepted. 'Neither of these is a host'
    # is not a reason to treat them as the same host.
    #
    # One operand, not two: `not server_host` was in the first version of this
    # fix and was removed because it does no work. If exactly one side is
    # empty, the inequality already refuses; both empty is the only case
    # needing a guard, and either guard alone catches it. Measured, not
    # reasoned -- with `not id_host` dropped the D32 test fails, and the
    # two-operand form left both single-operand mutants surviving.
    # Registered as D32, which filed the reachability as
    # unproven because it needed httpx to fetch a URL urlparse rejects; such a
    # URL exists -- a '[' or an NFKC-confusable in the USERINFO, which urlparse
    # rejects as part of the netloc while httpx strips userinfo and keeps the
    # real host, so httpx fetches 'banned.example' from
    # 'https://[@banned.example/u/alice' while every urlparse-derived value
    # here is ''. create_actor_from_remote is the caller that can deliver an
    # empty `server`: it takes it from extract_domain_and_actor, which returns
    # ('', '') on that ValueError, then fetches with a different variable.
    id_host = host_of(activity_json['id'])
    server_host = host_of(f'//{server}')
    if not id_host or id_host != server_host:
        return None
    # G1. Castopod's podcast actor has the custom type 'Podcast'; it authors Notes like a person.
    if activity_json['type'] in ('Person', 'Service', 'Podcast'):
        user = db.session.query(User).filter(User.ap_profile_id == activity_json['id'].lower()).first()
        if user:
            return user
        # D1372. `except KeyError` below catches this value being absent and
        # nothing else, while the same untrusted value being a number or a list
        # raised AttributeError straight out of this function, and one wider than
        # the column was a DataError at the caller's commit. Refusing here gives
        # all three the outcome the guard already intended for one of them.
        actor_name = actor_name_from_ap(activity_json)
        if not actor_name:
            current_app.logger.error(
                f'No usable preferredUsername for {address}@{server} in ' + str(activity_json))
            return None
        # D1372. `activity_json['publicKey']['publicKeyPem']` by hand, guarded by
        # the same `except KeyError`: a `publicKey` that is a string or a number
        # raised TypeError past it, and `{'publicKeyPem': None}` stored the STRING
        # 'None' as the actor's key, which no signature can ever verify against --
        # D1354's finding, fixed then for the refresh tasks and not for creation.
        # An actor with no verifiable key is not an actor this instance can accept.
        actor_pem = public_key_pem(activity_json)
        if not actor_pem:
            current_app.logger.error(
                f'No usable publicKey for {address}@{server} in ' + str(activity_json))
            return None
        try:
            user = User(user_name=actor_name,
                        title=actor_name_from_ap(activity_json, 'name', limit=256),
                        email=f"{address}@{server}",
                        matrix_user_id=activity_json['matrixUserId'] if 'matrixUserId' in activity_json else '',
                        indexable=activity_json['indexable'] if 'indexable' in activity_json else True,
                        searchable=activity_json['discoverable'] if 'discoverable' in activity_json else True,
                        # D1347. A peer's string straight into a DateTime column:
                        # `published: "whenever"` was a DataError at commit, so a
                        # remote actor whose actor document carries an unreadable
                        # `published` could never be created here -- and nothing
                        # they ever posted could land either.
                        created=parse_ap_timestamp(activity_json.get('published')) or utcnow(),
                        ap_id=f"{address.lower()}@{server.lower()}",
                        ap_public_url=activity_json['id'],
                        ap_profile_id=activity_json['id'].lower(),
                        ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'] if 'inbox' in activity_json else '',
                        ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                        ap_preferred_username=actor_name,
                        ap_manually_approves_followers=activity_json['manuallyApprovesFollowers'] if 'manuallyApprovesFollowers' in activity_json else False,
                        ap_fetched_at=utcnow(),
                        ap_domain=server.lower(),
                        public_key=actor_pem,
                        bot=True if activity_json['type'] == 'Service' else False,
                        instance_id=find_instance_id(server),
                        accept_private_messages=activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
                        # language=community_json['language'][0]['identifier'] # todo: language
                        )
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None

        if 'summary' in activity_json:
            about_html = _as_text(activity_json['summary'])  # D1355
            if about_html is not None and not about_html.startswith('<'):  # PeerTube
                about_html = '<p>' + about_html + '</p>'
            user.about_html = allowlist_html(about_html)
        else:
            user.about_html = ''
        source_markdown = markdown_source(activity_json)  # D1346
        if source_markdown is not None:
            user.about = source_markdown
            user.about_html = markdown_to_html(user.about)          # prefer Markdown if provided, overwrite version obtained from HTML
        else:
            user.about = html_to_text(user.about_html)

        if user.title and user.title.strip().lower() == '[deleted]':
            user.title = ''

        icon_entry = image_url_from(activity_json.get('icon'), prefer_last=True)
        if icon_entry:
            avatar = File(source_url=icon_entry)
            user.avatar = avatar
            db.session.add(avatar)
        # The list arm is bridgy-fed's, which sends `image` as a list.
        cover_entry = image_url_from(activity_json.get('image'))
        if cover_entry:
            cover = File(source_url=cover_entry)
            user.cover = cover
            db.session.add(cover)
        if 'attachment' in activity_json:
            # D1354, the second copy. `shorten_string` stays: this site shortens the
            # LABEL for display, which the refresh task does not, and that
            # difference is deliberate rather than a second reading of the key.
            user.extra_fields = [
                UserExtraField(label=shorten_string(label), text=text)
                for label, text in property_value_fields(activity_json['attachment'])]
        try:
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return db.session.query(User).filter_by(ap_profile_id=activity_json['id'].lower()).one()
        if user.avatar_id and get_setting('cache_remote_images_locally', True):
            make_image_sizes(user.avatar_id, 40, 250, 'users')
        if user.cover_id and get_setting('cache_remote_images_locally', True):
            make_image_sizes(user.cover_id, 878, None, 'users')
        return user
    elif activity_json['type'] == 'Group':
        community = db.session.query(Community).filter(Community.ap_profile_id == activity_json['id'].lower()).first()
        if community:
            return community
        if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy and mbin
            mods_url = activity_json['attributedTo']
        elif 'moderators' in activity_json and isinstance(activity_json['moderators'], str):  # kbin
            mods_url = activity_json['moderators']
        else:
            mods_url = None

        # only allow nsfw communities if enabled for this instance
        site = db.session.get(Site, 1)  # can't use g.site because actor_json_to_model can be called from celery
        if 'sensitive' in activity_json and activity_json['sensitive'] and not site.enable_nsfw:
            return None
        if 'nsfl' in activity_json and activity_json['nsfl'] and not site.enable_nsfl:
            return None

        # Read before the try, for two reasons. It is the only argument below
        # that can raise and is NOT peer data, so catching it in a handler that
        # logs 'while parsing <the peer's JSON>' names the wrong party and
        # sends the reader to inspect a document that is fine. And config.py
        # always sets this key (with an `or -1` fallback), so a deployment
        # missing it is broken in a way its operator needs to see rather than
        # have swallowed into a None the caller reads as 'malformed peer'.
        # Registered as D33.
        content_retention = current_app.config['DEFAULT_CONTENT_RETENTION']

        # D1372, as for the Person branch above. `title` was read with no guard at
        # all, so a Group document carrying no `name` -- which the Person branch
        # treats as ordinary -- could not be created here either; it falls back to
        # the actor's own name rather than refusing the community.
        actor_name = actor_name_from_ap(activity_json)
        if not actor_name:
            current_app.logger.error(
                f'No usable preferredUsername for {address}@{server} in ' + str(activity_json))
            return None
        # D1372. `activity_json['publicKey']['publicKeyPem']` by hand, guarded by
        # the same `except KeyError`: a `publicKey` that is a string or a number
        # raised TypeError past it, and `{'publicKeyPem': None}` stored the STRING
        # 'None' as the actor's key, which no signature can ever verify against --
        # D1354's finding, fixed then for the refresh tasks and not for creation.
        # An actor with no verifiable key is not an actor this instance can accept.
        actor_pem = public_key_pem(activity_json)
        if not actor_pem:
            current_app.logger.error(
                f'No usable publicKey for {address}@{server} in ' + str(activity_json))
            return None
        try:
            community = Community(name=actor_name,
                                  title=actor_name_from_ap(activity_json, 'name',
                                                           limit=256) or actor_name,
                                  nsfw=activity_json['sensitive'] if 'sensitive' in activity_json else False,
                                  ai_generated=activity_json['genAI'] if 'genAI' in activity_json else False,
                                  restricted_to_mods=activity_json['postingRestrictedToMods'] if 'postingRestrictedToMods' in activity_json else False,
                                  new_mods_wanted=activity_json['newModsWanted'] if 'newModsWanted' in activity_json else False,
                                  private_mods=activity_json['privateMods'] if 'privateMods' in activity_json else False,
                                  question_answer=activity_json['questionAnswer'] if 'questionAnswer' in activity_json else False,
                                  # D1374. The key is right here; the type and the
                                  # width were not. String(15).
                                  default_post_type=_as_text(
                                      activity_json.get('defaultPostType'), 15) or 'link',
                                  # D1347, on a community rather than an actor.
                                  created_at=parse_ap_timestamp(activity_json.get('published')) or utcnow(),
                                  last_active=parse_ap_timestamp(activity_json.get('updated')) or utcnow(),
                                  # D1377, as on the refresh path above.
                                  posting_warning=posting_warning_from_ap(activity_json),
                                  ap_id=f"{address[1:].lower()}@{server.lower()}" if address.startswith('!') else f"{address.lower()}@{server.lower()}",
                                  ap_public_url=activity_json['id'],
                                  ap_profile_id=activity_json['id'].lower(),
                                  ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                                  ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'] if 'inbox' in activity_json else '',
                                  ap_outbox_url=activity_json['outbox'],
                                  ap_featured_url=activity_json['featured'] if 'featured' in activity_json else '',
                                  ap_moderators_url=mods_url,
                                  ap_fetched_at=utcnow(),
                                  ap_domain=server.lower(),
                                  public_key=actor_pem,
                                  # language=community_json['language'][0]['identifier'] # todo: language
                                  content_retention=content_retention,
                                  first_federated_at=utcnow(),
                                  post_url_type=activity_json['postUrlType'] if 'postUrlType' in activity_json else None,
                                  )
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None

        # Assigned after the construction rather than inside it. find_instance_id
        # COMMITS a sparse Instance row and spawns a new_instance_profile fetch
        # when the peer is new, and keyword arguments evaluate in source order,
        # so while it sat mid-list any later argument that raised into the
        # handler above left that row and that fetch behind for a peer no
        # Community was ever created for. Moving it here makes the side effect
        # unreachable until there IS a Community to attach it to, and keeps it
        # that way no matter what is added to the argument list later -- which
        # ordering alone would not. The Person and Feed branches carry the same
        # call in the same position and are unaffected today only because every
        # argument after theirs is a guarded read or a literal. Registered as
        # D33.
        community.instance_id = find_instance_id(server)
        if get_setting('meme_comms_low_quality', False):
            community.low_quality = 'memes' in actor_name or 'shitpost' in actor_name
        description_html = ''
        description_html = _as_text(activity_json.get('summary')) or \
            _as_text(activity_json.get('content')) or ''  # D1355

        community.show_popular = db.session.get(Instance, community.instance_id).popular
        community.show_all = not db.session.get(Instance, community.instance_id).silenced

        if description_html is not None and description_html != '':
            if not description_html.startswith('<'):  # PeerTube
                description_html = '<p>' + description_html + '</p>'
            community.description_html = allowlist_html(description_html)
            source_markdown = markdown_source(activity_json)  # D1346
            if source_markdown is not None:
                community.description = source_markdown
                community.description_html = markdown_to_html(community.description)          # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                community.description = html_to_text(community.description_html)

        if 'theme' in activity_json and activity_json['theme']:
            community.theme = activity_json['theme']

        icon_entry = image_url_from(activity_json.get('icon'), prefer_last=True)
        if icon_entry:
            icon = File(source_url=icon_entry)
            community.icon = icon
            db.session.add(icon)
        image_entry = image_url_from(activity_json.get('image'))
        if image_entry:
            image = File(source_url=image_entry)
            community.image = image
            db.session.add(image)
        if 'language' in activity_json and isinstance(activity_json['language'], list):
            for ap_language in activity_json['language']:
                language = language_from_ap(ap_language)  # D1355
                if language is not None:
                    community.languages.append(find_language_or_create(*language))
        try:
            db.session.add(community)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return db.session.query(Community).filter_by(ap_profile_id=activity_json['id'].lower()).one()
        if 'tag' in activity_json and isinstance(activity_json['tag'], list):
            # New-style post flair
            community.flair = []
            for flair in activity_json["tag"]:
                if not isinstance(flair, dict) or "type" not in flair:
                    # An entry that is not an object, or an object with no
                    # 'type', has nothing for the test below to read: it used
                    # to raise TypeError and KeyError respectively, out of
                    # actor_json_to_model and after the Community above had
                    # already been committed. Skip it the way the sibling
                    # legacy loop skips an entry with no 'display_name'.
                    current_app.logger.warning(
                        f"actor_json_to_model: skipping a 'tag' entry of "
                        f"{activity_json['id']} -- it is not an object carrying "
                        f"a 'type'")
                    continue
                if flair["type"] == "CommunityPostTag":
                    flair_dict = flair
                    flair_obj = find_flair_or_create(flair_dict, community.id)
                    if flair_obj:
                        community.flair.append(flair_obj)
            db.session.commit()
        elif 'lemmy:tagsForPosts' in activity_json and isinstance(activity_json['lemmy:tagsForPosts'], list):
            # Legacy post flair
            community.flair = []
            for flair in activity_json['lemmy:tagsForPosts']:
                if not isinstance(flair, dict) or 'display_name' not in flair:
                    # An entry that is not an object, or an object with no
                    # 'display_name', has nothing to name the flair by: reading
                    # flair['display_name'] used to raise TypeError and KeyError
                    # respectively, out of actor_json_to_model and after the Community
                    # above had already been committed. Testing membership first is not
                    # enough, because `'display_name' not in flair` is itself a
                    # TypeError for a non-container and a plain substring test for a
                    # string, so the isinstance test has to come first and has to be
                    # here. Skip the entry the way the four optional keys below it are
                    # skipped, and say so, rather than dropping it silently.
                    current_app.logger.warning(
                        f"actor_json_to_model: skipping the 'lemmy:tagsForPosts' entry "
                        f"{flair!r} of {activity_json['id']} -- it is not an object "
                        f"carrying a 'display_name'")
                    continue
                flair_dict = {'display_name': flair['display_name']}
                if 'text_color' in flair:
                    flair_dict['text_color'] = flair['text_color']
                if 'background_color' in flair:
                    flair_dict['background_color'] = flair['background_color']
                if 'blur_images' in flair:
                    flair_dict['blur_images'] = flair['blur_images']
                if 'id' in flair:
                    flair_dict['id'] = flair['id']
                flair_obj = find_flair_or_create(flair_dict, community.id)
                if flair_obj:
                    community.flair.append(flair_obj)
            db.session.commit()
        if community.icon_id:
            make_image_sizes(community.icon_id, 60, 250, 'communities')
        if community.image_id:
            make_image_sizes(community.image_id, 700, 1600, 'communities')

        # Fire plugin hook for a new remote community
        plugins.fire_hook("new_remote_community", community)

        return community
    elif activity_json['type'] == 'Feed':
        feed = db.session.query(Feed).filter(Feed.ap_profile_id == activity_json['id'].lower()).first()
        if feed:
            return feed
        if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str):  # lemmy, mbin, and our feeds
            owners_url = activity_json['attributedTo']
        elif 'moderators' in activity_json and isinstance(activity_json['moderators'], str):  # kbin, and our feeds
            owners_url = activity_json['moderators']
        else:
            owners_url = None

        # only allow nsfw communities if enabled for this instance
        site = db.session.get(Site, 1)  # can't use g.site because actor_json_to_model can be called from celery
        if 'sensitive' in activity_json and activity_json['sensitive'] and not site.enable_nsfw:
            return None
        if 'nsfl' in activity_json and activity_json['nsfl'] and not site.enable_nsfl:
            return None

        # get the owners list
        # these users will be added to feedmember db entries at the bottom of this function
        if owners_url is None:
            # The third arm of the owners_url choice above, taken also by a
            # 'moderators' key that is not a string (D233), leaves owners_url
            # as None. Passing
            # that to get_request used to be the only way this branch reported
            # the problem, and it reported it inconsistently: with DEBUG off
            # is_invalid_get_request_uri refused the uri and get_request raised
            # httpx.HTTPError, while with DEBUG on that check short-circuits to
            # False and the `in uri` membership test a line later raised
            # TypeError instead. Refusing here makes the outcome the same in
            # both modes, and the same shape as the branch's other refusals.
            current_app.logger.error(
                f"actor_json_to_model: {activity_json['id']} names no owners "
                f"collection -- it has neither a string attributedTo nor a "
                f"moderators url, so the feed has no owner to belong to")
            return None
        owner_users = []
        owners_data = get_request(owners_url, headers={'Accept': 'application/activity+json'})
        if owners_data.status_code == 200:
            owners_json = owners_data.json()
            for owner in owners_json['orderedItems']:
                owner_user = find_actor_or_create(owner)
                if owner_user is None:
                    # The resolver refused this entry, so there is no user to
                    # own the feed or to build a FeedMember from. Skip it here
                    # rather than letting the None reach `owner_users[0].id`
                    # below, or `ou.id` in the FeedMember loop that runs after
                    # the Feed has been committed.
                    current_app.logger.warning(
                        f"actor_json_to_model: skipping {owner} in the owners "
                        f"collection of {activity_json['id']} -- it does not "
                        f"resolve to a user")
                    continue
                owner_users.append(owner_user)
        if not owner_users:
            # `user_id=owner_users[0].id` in the Feed() call below indexes this
            # list unconditionally. It is empty whenever the status guard above
            # took its false arm, whenever orderedItems was empty, and whenever
            # every entry in it was skipped. A feed with no owner cannot be
            # built, so refuse before anything is written -- nothing has been
            # committed at this point, so no partial row is left behind.
            current_app.logger.error(
                f"actor_json_to_model: the owners collection at {owners_url} "
                f"yielded no resolvable user for {activity_json['id']}")
            return None

        # also get the communities in the remote feed's /following list 
        feed_following = []
        try:
            following_data = get_request(activity_json['following'], headers={'Accept': 'application/activity+json'})
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None
        if following_data.status_code == 200:
            following_json = following_data.json()
            for c_ap_id in following_json['items']:
                community = find_actor_or_create(c_ap_id, community_only=True)
                if community is None:
                    # The resolver refused this entry, so there is no community to
                    # build a FeedItem from. Skip it here rather than letting the
                    # None reach `c.id` in the FeedItem loop, which runs after the
                    # Feed above has already been committed.
                    current_app.logger.warning(
                        f"actor_json_to_model: skipping {c_ap_id} in the /following "
                        f"collection of {activity_json['id']} -- it does not resolve "
                        f"to a community")
                    continue
                feed_following.append(community)

        # D1372, as for the Person and Group branches. `machine_name` was the raw
        # value while `name` was stripped, and `/f/<name>` looks a feed up by
        # `machine_name` -- so a peer publishing ` news ` gave this instance a feed
        # it could not serve at its own address (fact 781).
        # `Feed.machine_name` is String(50) where `Feed.name` is String(256), and
        # both are written from this one value, so 50 is the width that fits. A peer
        # publishing a 60-character preferredUsername was a DataError at the commit
        # below -- no remote feed of that name could be created at all.
        actor_name = actor_name_from_ap(activity_json, limit=50)
        if not actor_name:
            current_app.logger.error(
                f'No usable preferredUsername for {address}@{server} in ' + str(activity_json))
            return None
        # D1372. `activity_json['publicKey']['publicKeyPem']` by hand, guarded by
        # the same `except KeyError`: a `publicKey` that is a string or a number
        # raised TypeError past it, and `{'publicKeyPem': None}` stored the STRING
        # 'None' as the actor's key, which no signature can ever verify against --
        # D1354's finding, fixed then for the refresh tasks and not for creation.
        # An actor with no verifiable key is not an actor this instance can accept.
        actor_pem = public_key_pem(activity_json)
        if not actor_pem:
            current_app.logger.error(
                f'No usable publicKey for {address}@{server} in ' + str(activity_json))
            return None
        try:
            feed = Feed(name=actor_name,
                        user_id=owner_users[0].id,
                        title=actor_name_from_ap(activity_json, 'name',
                                                 limit=256) or actor_name,
                        nsfw=activity_json['sensitive'] if 'sensitive' in activity_json else False,
                        machine_name=actor_name,
                        # The real value is derived below, from `summary` or
                        # `content`, and put through allowlist_html. Assigning the
                        # peer's `summary` here as well read as the value being
                        # kept: for a non-string it WAS kept, because the branch
                        # below skips a value _as_text refuses, so a peer sending
                        # `summary: {}` left a dict in a Text column and the commit
                        # raised where no `except KeyError` could see it.
                        description_html='',
                        description=piefed_markdown_to_lemmy_markdown(
                            markdown_source(activity_json, require_media_type=False) or ''),  # D1346
                        # D1347, on a feed.
                        created_at=parse_ap_timestamp(activity_json.get('published')) or utcnow(),
                        last_edit=parse_ap_timestamp(activity_json.get('updated')) or utcnow(),
                        num_communities=0,
                        ap_id=f"{address[1:].lower()}@{server.lower()}" if address.startswith('~') else f"{address.lower()}@{server.lower()}",
                        ap_public_url=activity_json['id'],
                        ap_profile_id=activity_json['id'].lower(),
                        ap_followers_url=activity_json['followers'] if 'followers' in activity_json else None,
                        # Read unconditionally by the /following fetch above, so
                        # it is always present here (D18).
                        ap_following_url=activity_json['following'],
                        ap_inbox_url=activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json else activity_json['inbox'] if 'inbox' in activity_json else '',
                        ap_outbox_url=activity_json['outbox'],
                        ap_moderators_url=owners_url,
                        ap_fetched_at=utcnow(),
                        ap_domain=server.lower(),
                        public_key=actor_pem,
                        instance_id=find_instance_id(server),
                        public=True
                        )
        except KeyError:
            current_app.logger.error(f'KeyError for {address}@{server} while parsing ' + str(activity_json))
            return None

        description_html = ''
        description_html = _as_text(activity_json.get('summary')) or \
            _as_text(activity_json.get('content')) or ''  # D1355

        if description_html is not None and description_html != '':
            if not description_html.startswith('<'):  # PeerTube
                description_html = '<p>' + description_html + '</p>'
            feed.description_html = allowlist_html(description_html)
            source_markdown = markdown_source(activity_json)  # D1346
            if source_markdown is not None:
                feed.description = source_markdown
                feed.description_html = markdown_to_html(feed.description)  # prefer Markdown if provided, overwrite version obtained from HTML
            else:
                feed.description = html_to_text(feed.description_html)

        icon_entry = image_url_from(activity_json.get('icon'), prefer_last=True)
        if icon_entry:
            icon = File(source_url=icon_entry)
            feed.icon = icon
            db.session.add(icon)
        image_entry = image_url_from(activity_json.get('image'))
        if image_entry:
            image = File(source_url=image_entry)
            feed.image = image
            db.session.add(image)

        try:
            db.session.add(feed)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return db.session.query(Feed).filter_by(ap_profile_id=activity_json['id'].lower()).one()

        # add the owners as feedmembers
        for ou in owner_users:
            fm = FeedMember(feed_id=feed.id, user_id=ou.id, is_owner=True)
            db.session.add(fm)
            db.session.commit()

        # add the communities from the remote /following list as feeditems
        for c in feed_following:
            fi = FeedItem(feed_id=feed.id,
                        community_id=c.id)
            feed.num_communities += 1
            db.session.add(fi)
            db.session.commit()

        if feed.icon_id:
            make_image_sizes(feed.icon_id, 60, 250, 'feeds')
        if feed.image_id:
            make_image_sizes(feed.image_id, 700, 1600, 'feeds')

        if 'childFeeds' in activity_json:
            if isinstance(activity_json['childFeeds'], list):
                for child_feed in activity_json['childFeeds']:
                    populate_child_feed(feed.id, child_feed)
            else:
                # Only a list can be iterated as the collection of child feed
                # urls this loop means, and the three ways of getting that
                # wrong failed three different ways, all of them after the
                # Feed, its FeedMembers and its FeedItems had been committed.
                # null and any other scalar raised TypeError out of
                # actor_json_to_model. A string did not raise at all: it
                # iterated its own characters, and each single character
                # reached populate_child_feed, which resolves '~<char>@' and
                # reparents whatever feed answers to it -- in production N
                # failing celery tasks nobody reads, inline and visible only
                # under DEBUG. A mapping did not raise either: it iterated its
                # keys, so a peer could link child feeds through an object
                # this code never meant to accept. The isinstance test matches
                # the Group branch's `'tag' in activity_json and
                # isinstance(activity_json['tag'], list)`, and the warning is
                # here because the two silent cases would otherwise stay
                # silent.
                current_app.logger.warning(
                    f"actor_json_to_model: ignoring the 'childFeeds' of "
                    f"{activity_json['id']} -- it is a "
                    f"{type(activity_json['childFeeds']).__name__}, not a list, "
                    f"so no child feed is linked to this feed")
        return feed


MAX_GALLERY_IMAGES = 20  # extra images kept per post; a peer chooses how many it sends


def gallery_attachments(request_json: dict, primary_urls) -> list:
    """The image attachments of a peer's object other than the post's own image.

    Pixelfed and Mastodon send an album as several `attachment` entries
    (`Document` or `Image`, `mediaType` image/*, `name` the alt text). Post.new
    keeps the first as post.url/post.image; the rest are returned here, in order,
    as {'url', 'alt_text', 'width', 'height'}. Only http(s) urls are kept.
    """
    attachments = request_json['object'].get('attachment')
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list):
        return []
    if any(_as_dict(attachment).get('type') == 'Link' for attachment in attachments):
        return []  # a link post (Mbin sends the image beside it): the images are its preview, not an album
    seen = {url for url in primary_urls if url}
    found = []
    for attachment in attachments:
        attachment = _as_dict(attachment)
        if attachment.get('type') not in ('Image', 'Document'):
            continue
        url = _as_url(attachment.get('url'), 1024)
        if not url or url in seen or not url_is_storable(url):
            continue
        # No is_image_url() fallback: it HEADs the peer for an extensionless url
        media_type = attachment.get('mediaType')
        if attachment['type'] != 'Image' and not (isinstance(media_type, str) and media_type.startswith('image/')):
            continue
        seen.add(url)
        alt_text = attachment.get('name')
        found.append({'url': url,
                      'alt_text': alt_text[:1500] if isinstance(alt_text, str) and alt_text else None,
                      'width': _as_int(attachment.get('width'), None),
                      'height': _as_int(attachment.get('height'), None)})
    return found[:MAX_GALLERY_IMAGES]


def gallery_image_is_blocked(image: dict) -> bool:
    """Whether a gallery image (an entry of gallery_attachments) matches a blocked
    image, hashing it as Post.new hashes the post's own image. The hash is kept in
    image['hash'] (None when no IMAGE_HASHING_ENDPOINT is configured or the
    endpoint gave none), so an image is hashed once."""
    if 'hash' not in image:
        image['hash'] = retrieve_image_hash(image['url']) if current_app.config['IMAGE_HASHING_ENDPOINT'] else None
    return bool(image['hash']) and hash_matches_blocked_image(image['hash'])


def set_post_gallery(post: Post, request_json: dict, low_quality: bool = False, images: list = None):
    """Make `post.gallery` the extra images of `request_json`, replacing what it held.

    Each image is sized by make_image_sizes, as the post's own image is, and the
    `weight` of the post_file row is its place in the album (the post's own image
    being 0). `images` is gallery_attachments' answer when the caller already has
    it (Post.new hashes them before the post exists). An image matching a blocked
    image is not stored: Post.new refuses the whole post for one, so this only
    drops one an Update brings.
    """
    old_ids = [row.file_id for row in db.session.execute(
        post_file.select().where(post_file.c.post_id == post.id)).all()]
    db.session.execute(post_file.delete().where(post_file.c.post_id == post.id))
    for file in File.query.filter(File.id.in_(old_ids)).all():
        file.delete_from_disk()
        db.session.delete(file)

    if post.type == POST_TYPE_IMAGE:
        if images is None:
            images = gallery_attachments(request_json, [post.url, post.image.source_url if post.image else None])
        images = [extra for extra in images if not gallery_image_is_blocked(extra)]
        for weight, extra in enumerate(images, start=1):
            file = File(source_url=extra['url'], alt_text=extra['alt_text'],
                        width=extra['width'], height=extra['height'], hash=extra['hash'])
            db.session.add(file)
            db.session.flush()
            db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
            if get_setting('cache_remote_images_locally', True):
                make_image_sizes(file.id, 512, 1200, 'posts', low_quality)
    db.session.commit()


# Save two different versions of a File, after downloading it from file.source_url. Set a width parameter to None to avoid generating one of that size
def make_image_sizes(file_id, thumbnail_width=50, medium_width=120, directory='posts', toxic_community=False):
    if current_app.debug:
        make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community)
    else:
        make_image_sizes_async.apply_async(args=(file_id, thumbnail_width, medium_width, directory, toxic_community),
                                           countdown=randint(1, 10))  # Delay by up to 10 seconds so servers do not experience a stampede of requests all in the same second


@celery.task
def make_image_sizes_async(file_id, thumbnail_width, medium_width, directory, toxic_community):
    with current_app.app_context():
        original_directory = directory
        session = get_task_session()
        try:
            with patch_db_session(session):
                file: File = session.get(File, file_id)
                if file and file.source_url:
                    if file.source_url.endswith('.gif'):    # don't resize gifs, it breaks their animation
                        return
                    try:
                        source_image_response = get_request(file.source_url)
                    except:
                        pass
                    else:
                        if (source_image_response.status_code == 404 or source_image_response.status_code == 500) and '/api/v3/image_proxy' in file.source_url:
                            source_image_response.close()
                            # Lemmy failed to retrieve the image but we might have better luck. Example source_url: https://slrpnk.net/api/v3/image_proxy?url=https%3A%2F%2Fi.guim.co.uk%2Fimg%2Fmedia%2F24e87cb4d730141848c339b3b862691ca536fb26%2F0_164_3385_2031%2Fmaster%2F3385.jpg%3Fwidth%3D1200%26height%3D630%26quality%3D85%26auto%3Dformat%26fit%3Dcrop%26overlay-align%3Dbottom%252Cleft%26overlay-width%3D100p%26overlay-base64%3DL2ltZy9zdGF0aWMvb3ZlcmxheXMvdGctZGVmYXVsdC5wbmc%26enable%3Dupscale%26s%3D0ec9d25a8cb5db9420471054e26cfa63
                            # The un-proxied image url is the query parameter called 'url'
                            parsed_url = urlparse(file.source_url)
                            query_params = parse_qs(parsed_url.query)
                            if 'url' in query_params:
                                url_value = query_params['url'][0]
                                source_image_response = get_request(url_value)
                            else:
                                source_image_response = None
                        if source_image_response and source_image_response.status_code == 200:
                            content_type = source_image_response.headers.get('content-type')
                            if content_type:
                                if content_type.startswith('image') or (content_type == 'application/octet-stream' and file.source_url.endswith('.avif')):
                                    source_image = source_image_response.content
                                    source_image_response.close()

                                    # detect AI image posts
                                    if directory == 'posts':
                                        if ai_image := inspect_image_c2pa(source_image, content_type):
                                            if ai_image['c2pa']['ai_generated']:
                                                session.execute(text('UPDATE "post" SET ai_generated = true WHERE image_id = :file_id AND ai_generated is false'), {
                                                    'file_id': file.id
                                                })

                                    # content type headers often are just 'image/jpeg' but sometimes 'image/jpeg;charset=utf8'
                                    # `str.split('/')` always returns a
                                    # non-empty list, so the `if
                                    # content_type_parts:` that used to wrap
                                    # this -- with an `else` deriving the
                                    # extension from the url instead -- could
                                    # never take its second arm.

                                    # Remove ;charset=whatever
                                    main_part = content_type.split(';')[0]

                                    # Split the main part on the '/' character and take the second part
                                    file_ext = '.' + main_part.split('/')[1].lower()
                                    file_ext = file_ext.strip()  # just to be sure

                                    if file_ext == '.jpeg':
                                        file_ext = '.jpg'
                                    elif file_ext == '.svg+xml':
                                        return  # no need to resize SVG images
                                    elif file_ext == '.octet-stream':
                                        file_ext = '.avif'

                                    new_filename = gibberish(15)

                                    # set up the storage directory
                                    if store_files_in_s3():
                                        directory = 'app/static/tmp'
                                    else:
                                        directory = f'app/static/media/{directory}/' + new_filename[0:2] + '/' + new_filename[2:4]
                                    ensure_directory_exists(directory)

                                    # file path and names to store the resized images on disk
                                    final_place = os.path.join(directory, new_filename + file_ext)
                                    final_place_thumbnail = os.path.join(directory, new_filename + '_thumbnail.webp')

                                    if file_ext == '.avif':  # this is quite a big package so we'll only load it if necessary
                                        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

                                    # Load image data into Pillow
                                    image = Image.open(BytesIO(source_image))
                                    image = ImageOps.exif_transpose(image)
                                    img_width = image.width

                                    boto3_session = None
                                    s3 = None

                                    # Use environment variables to determine medium and thumbnail format and quality.
                                    # But for communities and users directories, preserve original file type.
                                    if original_directory in ['communities', 'users']:
                                        # Preserve original format by using the file extension
                                        if file_ext.lower() in ['.jpg', '.jpeg']:
                                            medium_image_format = 'JPEG'
                                            thumbnail_image_format = 'JPEG'
                                        elif file_ext.lower() == '.png':
                                            medium_image_format = 'PNG'
                                            thumbnail_image_format = 'PNG'
                                        elif file_ext.lower() == '.webp':
                                            medium_image_format = 'WEBP'
                                            thumbnail_image_format = 'WEBP'
                                        elif file_ext.lower() == '.avif':
                                            medium_image_format = 'AVIF'
                                            thumbnail_image_format = 'AVIF'
                                        else:
                                            # Default to PNG for other formats
                                            medium_image_format = 'PNG'
                                            thumbnail_image_format = 'PNG'
                                    else:
                                        medium_image_format = current_app.config['MEDIA_IMAGE_MEDIUM_FORMAT']
                                        thumbnail_image_format = current_app.config['MEDIA_IMAGE_THUMBNAIL_FORMAT']
                                    medium_image_quality = current_app.config['MEDIA_IMAGE_MEDIUM_QUALITY']
                                    thumbnail_image_quality = current_app.config['MEDIA_IMAGE_THUMBNAIL_QUALITY']

                                    final_ext = file_ext.lower()  # track file extension for conversion
                                    thumbnail_ext = file_ext.lower()

                                    if medium_image_format == 'AVIF' or thumbnail_image_format == 'AVIF':
                                        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

                                    # Resize the image to medium
                                    if medium_width:
                                        # `medium_image` is assigned here and
                                        # read unconditionally below. An image
                                        # already narrower than `medium_width`,
                                        # on an instance that has configured no
                                        # medium format, took neither arm and
                                        # the save was
                                        # `UnboundLocalError: cannot access
                                        # local variable 'medium_image'` -- so
                                        # no medium copy, no thumbnail and no
                                        # dimensions for any small image.
                                        medium_image = image.copy()
                                        if img_width > medium_width or medium_image_format:
                                            if (medium_image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']):
                                                medium_image = to_srgb(medium_image)
                                            else:
                                                medium_image = medium_image.convert('RGBA')
                                            medium_image.thumbnail((medium_width, sys.maxsize), resample=Image.LANCZOS)

                                        kwargs = {}
                                        if medium_image_format:
                                            kwargs['format'] = medium_image_format.upper()
                                            final_ext = '.' + medium_image_format.lower()
                                            final_place = os.path.splitext(final_place)[0] + final_ext
                                        if medium_image_quality:
                                            kwargs['quality'] = int(medium_image_quality)

                                        medium_image.save(final_place, optimize=True, **kwargs)

                                        if store_files_in_s3():
                                            content_type = guess_mime_type(final_place)
                                            extra_args = {'ContentType': content_type}
                                            if current_app.config.get('S3_STORAGE_CLASS'):
                                                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                                            if current_app.config.get('S3_PUBLIC_ACL'):
                                                extra_args['ACL'] = 'public-read'
                                            boto3_session = boto3.session.Session()
                                            s3 = boto3_session.client(
                                                service_name='s3',
                                                region_name=current_app.config['S3_REGION'],
                                                endpoint_url=current_app.config['S3_ENDPOINT'],
                                                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                                                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                                            )
                                            s3.upload_file(final_place, current_app.config['S3_BUCKET'],
                                                           original_directory + '/' +
                                                           new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                                                           ExtraArgs=extra_args)
                                            os.unlink(final_place)
                                            final_place = f"https://{current_app.config['S3_PUBLIC_URL']}/{original_directory}/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                                          '/' + new_filename + final_ext

                                        file.file_path = final_place
                                        file.width = medium_image.width
                                        file.height = medium_image.height

                                    # Resize the image to a thumbnail (webp)
                                    if thumbnail_width:
                                        thumbnail_image = image.copy()
                                        if thumbnail_image_format == 'JPEG':
                                            thumbnail_image = to_srgb(thumbnail_image)
                                        else:
                                            thumbnail_image = thumbnail_image.convert('RGBA')
                                        if img_width > thumbnail_width:
                                            thumbnail_image.thumbnail((thumbnail_width, thumbnail_width), resample=Image.LANCZOS)

                                        kwargs = {}
                                        if thumbnail_image_format:
                                            kwargs['format'] = thumbnail_image_format.upper()
                                            thumbnail_ext = '.' + thumbnail_image_format.lower()
                                            final_place_thumbnail = os.path.splitext(final_place_thumbnail)[0] + thumbnail_ext
                                        if thumbnail_image_quality:
                                            kwargs['quality'] = int(thumbnail_image_quality)

                                        thumbnail_image.save(final_place_thumbnail, optimize=True, **kwargs)

                                        if store_files_in_s3():
                                            content_type = guess_mime_type(final_place_thumbnail)
                                            extra_args = {'ContentType': content_type}
                                            if current_app.config.get('S3_STORAGE_CLASS'):
                                                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
                                            if current_app.config.get('S3_PUBLIC_ACL'):
                                                extra_args['ACL'] = 'public-read'
                                            if boto3_session is None and s3 is None:
                                                boto3_session = boto3.session.Session()
                                                s3 = boto3_session.client(
                                                    service_name='s3',
                                                    region_name=current_app.config['S3_REGION'],
                                                    endpoint_url=current_app.config['S3_ENDPOINT'],
                                                    aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                                                    aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
                                                )
                                            s3.upload_file(final_place_thumbnail, current_app.config['S3_BUCKET'],
                                                           original_directory + '/' +
                                                           new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + '_thumbnail' + thumbnail_ext,
                                                           ExtraArgs=extra_args)
                                            os.unlink(final_place_thumbnail)
                                            final_place_thumbnail = f"https://{current_app.config['S3_PUBLIC_URL']}/{original_directory}/{new_filename[0:2]}/{new_filename[2:4]}" + \
                                                                    '/' + new_filename + '_thumbnail' + thumbnail_ext
                                        file.thumbnail_path = final_place_thumbnail
                                        # `thumbnail_image`, not `image`. These
                                        # recorded the SOURCE image's size, so
                                        # every thumbnail this instance made was
                                        # described with the dimensions of the
                                        # full-size original -- the medium
                                        # branch above reads its own copy.
                                        file.thumbnail_width = thumbnail_image.width
                                        file.thumbnail_height = thumbnail_image.height

                                    if s3:
                                        s3.close()
                                    session.commit()

                                    site = session.get(Site, 1)
                                    if site is None:
                                        site = Site()

                                    # Alert regarding fascist meme content
                                    if site.enable_chan_image_filter and toxic_community and img_width < 2000:  # images > 2000px tend to be real photos instead of 4chan screenshots.
                                        if os.environ.get('ALLOW_4CHAN', None) is None:
                                            try:
                                                image_text = pytesseract.image_to_string(
                                                    Image.open(BytesIO(source_image)).convert('L'), timeout=30)
                                            except Exception:
                                                image_text = ''
                                            if 'Anonymous' in image_text and ('No.' in image_text or ' N0' in image_text):  # chan posts usually contain the text 'Anonymous' and ' No.12345'
                                                post = session.query(Post).filter_by(image_id=file.id).first()
                                                # D1391, the same producer/consumer
                                                # mismatch one subtype over: the
                                                # `post_with_suspicious_image` block
                                                # (app/templates/user/notifs/20.html:
                                                # 115-140) reads
                                                # `targets.suspect_user_user_name`
                                                # too, and this -- its only producer
                                                # -- did not write it, so its Author
                                                # line was blank as well. Found by
                                                # the sweep for the domain subtype's
                                                # writers, which caught this dict.
                                                targets_data = {'gen': '0',
                                                                'post_id': post.id,
                                                                'orig_post_title': post.title,
                                                                'orig_post_body': post.body,
                                                                'suspect_user_user_name': (
                                                                    post.author.ap_id if post.author and post.author.ap_id
                                                                    else post.author.user_name if post.author else ''),
                                                                }
                                                notification = Notification(title='Review this',
                                                                            user_id=1,
                                                                            author_id=post.user_id,
                                                                            url=post.slug,
                                                                            notif_type=NOTIF_REPORT,
                                                                            subtype='post_with_suspicious_image',
                                                                            targets=targets_data)
                                                session.add(notification)
                                                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def find_reply_parent(in_reply_to: str) -> Tuple[int, int, int]:
    parent_comment = post = None
    post_id = parent_comment_id = root_id = None

    # 'comment' is hint that in_reply_to was another comment
    if 'comment' in in_reply_to:
        parent_comment = PostReply.get_by_ap_id(in_reply_to)
        if parent_comment:
            parent_comment_id = parent_comment.id
            post_id = parent_comment.post_id
            root_id = parent_comment.root_id

    # 'post' is hint that in_reply_to was a post
    if not parent_comment and 'post' in in_reply_to:
        post = Post.get_by_ap_id(in_reply_to)
        if post:
            post_id = post.id

    # no hint in in_reply_to, or it was misleading (e.g. replies to nodebb comments have '/post/' in them)
    if not parent_comment and not post:
        parent_comment = PostReply.get_by_ap_id(in_reply_to)
        if parent_comment:
            parent_comment_id = parent_comment.id
            post_id = parent_comment.post_id
            root_id = parent_comment.root_id
        else:
            post = Post.get_by_ap_id(in_reply_to)
            if post:
                post_id = post.id

    return post_id, parent_comment_id, root_id


@cache.memoize(timeout=1200)  # 20 minutes
def _find_liked_object_id(ap_id: str) -> Union[Tuple[int, str], None]:
    """Cache only the object ID and type, not the full SQLAlchemy model.

    Returns a tuple of (id, class_name) or None if not found.
    This avoids Redis serialization issues with SQLAlchemy models.
    """

    if '/comment/' in ap_id:
        post_reply = db.session.query(PostReply.id).filter(PostReply.ap_id == ap_id).first()
        if post_reply and post_reply[0]:
            return (post_reply[0], 'PostReply')
    else:
        post = db.session.query(Post.id, Post.archived).filter(Post.ap_id == ap_id).first()
        if post and post[0]:
            if post[1]:
                return None
            return (post[0], 'Post')
        else:
            post_reply = db.session.query(PostReply.id).filter(PostReply.ap_id == ap_id).first()
            if post_reply and post_reply[0]:
                return (post_reply[0], 'PostReply')
    return None


def find_liked_object(ap_id) -> Union[Post, PostReply, None]:
    """Find a post or comment by ActivityPub ID (cached).

    This function caches the object ID in Redis and then fetches the fresh
    model from the database using primary key lookup for better performance.
    """
    # `'/comment/' in ap_id` below is `TypeError: argument of type 'NoneType'
    # is not iterable` for anything that is not a string, and the id comes
    # from `core_activity['object']['object']` on an Undo -- whatever the peer
    # put there, including nothing and including an object.
    if not isinstance(ap_id, str):
        return None
    # Try to get cached ID and type
    result = _find_liked_object_id(ap_id)

    if result:
        obj_id, obj_type = result
        # Fetch fresh model from database using primary key lookup (very fast)
        if obj_type == 'Post':
            post = db.session.get(Post, obj_id)
            if post and post.archived:
                return None
            return post
        elif obj_type == 'PostReply':
            return db.session.get(PostReply, obj_id)
    else:
        cache.delete_memoized(_find_liked_object_id, ap_id)

    return None


def find_reported_object(ap_id) -> Union[User, Post, PostReply, None]:
    post = Post.get_by_ap_id(ap_id)
    if post:
        return post
    else:
        post_reply = PostReply.get_by_ap_id(ap_id)
        if post_reply:
            return post_reply
        else:
            user = find_actor_or_create(ap_id, create_if_not_found=False)
            if user:
                return user
    return None


def find_instance_id(server):
    if not server:
        return None
    server = server.strip().lower()
    instance = db.session.query(Instance).filter_by(domain=server).first()
    if instance:
        return instance.id
    else:
        # Our instance does not know about {server} yet. Initially, create a sparse row in the 'instance' table and spawn a background
        # task to update the row with more details later
        new_instance = Instance(domain=server, software='unknown', inbox=f'https://{server}/inbox', created_at=utcnow())

        try:
            db.session.add(new_instance)
            db.session.commit()
        except IntegrityError:
            # D1425. This returned the Instance OBJECT while every other path in this function
            # returns an id -- so the one arm that fires when two inbox workers meet the same
            # new peer at once handed its caller something no caller expects. Every one of them
            # assigns the result to an `instance_id` and uses it as a foreign key or compares
            # it to `InstanceBan.instance_id`, which is a different value and a different type.
            db.session.rollback()
            return db.session.query(Instance).filter_by(domain=server).one().id

        # Spawn background task to fill in more details
        new_instance_profile(new_instance.id)

        return new_instance.id


def find_instance_by_domain(server):
    server = server.strip().lower()
    return db.session.query(Instance).filter_by(domain=server).first()


def known_instance_id(server):
    """The id of an instance this server already knows, or None. Unlike find_instance_id it never creates a row
    or spawns a fetch, so unauthenticated GETs can use it without writing to the database."""
    if not server:
        return None
    instance = find_instance_by_domain(server)
    return instance.id if instance else None


def new_instance_profile(instance_id: int):
    if instance_id:
        if current_app.debug:
            new_instance_profile_task(instance_id)
        else:
            new_instance_profile_task.apply_async(args=(instance_id,), countdown=randint(1, 10))


@celery.task
def new_instance_profile_task(instance_id: int):
    session = get_task_session()
    try:
        with patch_db_session(session):
            instance: Instance = session.get(Instance, instance_id)
            protocol = 'https'
            try:
                instance_data = get_request(f"{protocol}://{instance.domain}", headers={'Accept': 'application/activity+json'})
            except:
                try:
                    instance_data = get_request(f"http://{instance.domain}", headers={'Accept': 'application/activity+json'})
                    protocol = 'http'
                except:
                    return
            if instance_data.status_code == 200:
                try:
                    instance_json = instance_data.json()
                    instance_data.close()
                except Exception:
                    instance_json = {}
                if 'type' in instance_json and instance_json['type'] == 'Application':
                    instance.inbox = instance_json['inbox'] if 'inbox' in instance_json else f"{protocol}://{instance.domain}/inbox"
                    # `'outbox' in instance_json`, as the line above already
                    # does for the inbox: this is another instance's actor
                    # document, and one without an outbox was a KeyError that
                    # killed the task -- so nothing about that instance was
                    # ever learned, not even its software.
                    if 'outbox' in instance_json:
                        instance.outbox = instance_json['outbox']
                else:  # it's pretty much always /inbox so just assume that it is for whatever this instance is running
                    instance.inbox = f"{protocol}://{instance.domain}/inbox"
                instance.updated_at = utcnow()
                session.commit()

                # retrieve list of Admins from /api/v3/site, update InstanceRole
                try:
                    response = get_request(f'{protocol}://{instance.domain}/api/v3/site')
                except:
                    response = None

                if response and response.status_code == 200:
                    try:
                        instance_data = response.json()
                    except:
                        instance_data = None
                    finally:
                        response.close()

                    if instance_data:
                        if 'admins' in instance_data:
                            admin_profile_ids = []
                            for admin in instance_data['admins']:
                                # Whatever /api/v3/site answered. An entry
                                # without a person, or a person without an
                                # actor_id, was a KeyError.
                                if not isinstance(admin, dict) or not isinstance(admin.get('person'), dict) \
                                        or not admin['person'].get('actor_id'):
                                    continue
                                admin_profile_ids.append(admin['person']['actor_id'].lower())
                                user = find_actor_or_create(admin['person']['actor_id'], retry=True)
                                if user and not instance.user_is_admin(user.id):
                                    new_instance_role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
                                    session.add(new_instance_role)
                                    session.commit()
                            # remove any InstanceRoles that are no longer part of instance-data['admins']
                            for instance_admin in session.query(InstanceRole).filter_by(instance_id=instance.id):
                                if instance_admin.user.profile_id() not in admin_profile_ids:
                                    session.query(InstanceRole).filter(
                                        InstanceRole.user_id == instance_admin.user.id,
                                        InstanceRole.instance_id == instance.id,
                                        InstanceRole.role == 'admin').delete()
                                    session.commit()
            elif instance_data.status_code == 406 or instance_data.status_code == 404:  # Mastodon and PeerTube do 406, a.gup.pe does 404
                instance.inbox = f"{protocol}://{instance.domain}/inbox"
                instance.updated_at = utcnow()
                session.commit()

            headers = {'Accept': 'application/activity+json'}
            try:
                nodeinfo = get_request(f"{protocol}://{instance.domain}/.well-known/nodeinfo", headers=headers)
                if nodeinfo.status_code == 200:
                    nodeinfo_json = nodeinfo.json()
                    for links in nodeinfo_json['links']:
                        if isinstance(links, dict) and 'rel' in links and (
                                links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.0' or  # most platforms except KBIN and Lemmy v0.19.4
                                links['rel'] == 'https://nodeinfo.diaspora.software/ns/schema/2.0' or  # KBIN
                                links['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.1'):  # Lemmy v0.19.4+ (no 2.0 back-compat provided here)
                            try:
                                time.sleep(0.1)
                                node = get_request(links['href'], headers=headers)
                                if node.status_code == 200:
                                    node_json = node.json()
                                    if 'software' in node_json:
                                        instance.software = node_json['software']['name'].lower()
                                        instance.version = node_json['software']['version']
                                        instance.nodeinfo_href = links['href']
                                        session.commit()
                                        break  # most platforms (except Lemmy v0.19.4) that provide 2.1 also provide 2.0 - there's no need to check both
                            except:
                                return
            except:
                return
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')


def delete_post_or_comment(deletor, to_delete, store_ap_json, request_json, reason):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    community = to_delete.community
    if (to_delete.user_id == deletor.id or
            (deletor.instance_id == to_delete.author.instance_id and deletor.is_instance_admin()) or
            community.is_moderator(deletor) or
            community.is_instance_admin(deletor)):
        if isinstance(to_delete, Post):
            with app_pkg.redis_client.lock(f"lock:post:{to_delete.id}", timeout=10, blocking_timeout=6):
                to_delete.deleted = True
                to_delete.deleted_by = deletor.id
                db.session.commit()
                if to_delete.url and to_delete.cross_posts is not None:
                    to_delete.calculate_cross_posts(delete_only=True)
            with app_pkg.redis_client.lock(f"lock:community:{community.id}", timeout=10, blocking_timeout=6):
                community.post_count -= 1
                adjust_domain_post_count(to_delete, -1)  # D1362
            with app_pkg.redis_client.lock(f"lock:user:{to_delete.user_id}", timeout=10, blocking_timeout=6):
                to_delete.author.post_count -= 1
                db.session.commit()
            if to_delete.author.id != deletor.id:
                add_to_modlog('delete_post', actor=deletor, target_user=to_delete.author, reason=reason,
                              community=community, post=to_delete,
                              link_text=shorten_string(to_delete.title), link=f'post/{to_delete.id}')
            # remove any notifications about the post
            notifs = db.session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == to_delete.id)
            for notif in notifs:
                # dont delete report notifs
                if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
                    continue
                db.session.delete(notif)
            db.session.commit()
        elif isinstance(to_delete, PostReply):
            with app_pkg.redis_client.lock(f"lock:post_reply:{to_delete.id}", timeout=10, blocking_timeout=6):
                to_delete.deleted = True
                to_delete.deleted_by = deletor.id
                if to_delete.path and len(to_delete.path) > 1:
                    db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                                       {'parents': tuple(to_delete.path[:-1])})
                db.session.commit()
            with app_pkg.redis_client.lock(f"lock:user:{to_delete.user_id}", timeout=10, blocking_timeout=6):
                to_delete.author.post_reply_count -= 1
                db.session.commit()
                if not to_delete.author.bot:
                    with app_pkg.redis_client.lock(f"lock:post:{to_delete.post_id}", timeout=10, blocking_timeout=6):
                        to_delete.post.reply_count -= 1
                        if to_delete.post.reply_count_cross_posted:
                            to_delete.post.reply_count_cross_posted -= 1
                        db.session.commit()
            # D1361. `community.post_reply_count` used to be decremented out here,
            # outside the `if not ... .bot` gate -- but `PostReply.new` only
            # INCREMENTS it for a non-bot (app/models.py, `if not user.bot:`), and
            # `app/shared/reply.py`'s local delete keeps it inside the gate. So
            # every bot reply deleted through federation took one off a count it had
            # never been added to. Measured: a community at 0 went to -1.
            if not to_delete.author.bot:
                with app_pkg.redis_client.lock(f"lock:community:{community.id}", timeout=10, blocking_timeout=6):
                    community.post_reply_count -= 1
                    db.session.commit()

            if to_delete.author.id != deletor.id:
                add_to_modlog('delete_post_reply', actor=deletor, target_user=to_delete.author, reason=reason,
                              community=community, post=to_delete.post, reply=to_delete,
                              link_text=f'comment on {shorten_string(to_delete.post.title)}',
                              link=f'post/{to_delete.post.id}#comment_{to_delete.id}')
        log_incoming_ap(id, APLOG_DELETE, APLOG_SUCCESS, saved_json)
    else:
        log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Deletor did not have permisson')


def restore_post_or_comment(restorer, to_restore, store_ap_json, request_json, reason):
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    community = to_restore.community
    if (to_restore.user_id == restorer.id or
            (restorer.instance_id == to_restore.author.instance_id and restorer.is_instance_admin()) or
            community.is_moderator(restorer) or
            community.is_instance_admin(restorer)):
        # The same locks delete_post_or_comment takes around the same counters (D202)
        if isinstance(to_restore, Post):
            with app_pkg.redis_client.lock(f"lock:post:{to_restore.id}", timeout=10, blocking_timeout=6):
                to_restore.deleted = False
                to_restore.deleted_by = None
                if to_restore.url:
                    to_restore.calculate_cross_posts()
                db.session.commit()
            with app_pkg.redis_client.lock(f"lock:community:{community.id}", timeout=10, blocking_timeout=6):
                community.post_count += 1
                adjust_domain_post_count(to_restore, 1)  # D1362
            with app_pkg.redis_client.lock(f"lock:user:{to_restore.user_id}", timeout=10, blocking_timeout=6):
                to_restore.author.post_count += 1
                db.session.commit()
            if to_restore.author.id != restorer.id:
                add_to_modlog('restore_post', actor=restorer, target_user=to_restore.author, reason=reason,
                              community=community, post=to_restore,
                              link_text=shorten_string(to_restore.title), link=f'post/{to_restore.id}')

        elif isinstance(to_restore, PostReply):
            with app_pkg.redis_client.lock(f"lock:post_reply:{to_restore.id}", timeout=10, blocking_timeout=6):
                to_restore.deleted = False
                to_restore.deleted_by = None
                if to_restore.path and len(to_restore.path) > 1:
                    db.session.execute(text('update post_reply set child_count = child_count + 1 where id in :parents'),
                                       {'parents': tuple(to_restore.path[:-1])})
                db.session.commit()
            with app_pkg.redis_client.lock(f"lock:user:{to_restore.user_id}", timeout=10, blocking_timeout=6):
                to_restore.author.post_reply_count += 1
                db.session.commit()
                if not to_restore.author.bot:
                    with app_pkg.redis_client.lock(f"lock:post:{to_restore.post_id}", timeout=10, blocking_timeout=6):
                        to_restore.post.reply_count += 1
                        to_restore.post.reply_count_cross_posted += 1
                        db.session.commit()
            if not to_restore.author.bot:
                with app_pkg.redis_client.lock(f"lock:community:{community.id}", timeout=10, blocking_timeout=6):
                    community.post_reply_count += 1  # D1361, as in the delete above
                    db.session.commit()
            if to_restore.author.id != restorer.id:
                add_to_modlog('restore_post_reply', actor=restorer, target_user=to_restore.author, reason=reason,
                              community=community, post=to_restore.post, reply=to_restore,
                              link_text=f'comment on {shorten_string(to_restore.post.title)}',
                              link=f'post/{to_restore.post_id}#comment_{to_restore.id}')
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_SUCCESS, saved_json)
    else:
        log_incoming_ap(id, APLOG_UNDO_DELETE, APLOG_FAILURE, saved_json, 'Restorer did not have permisson')


def site_ban_remove_data(blocker_id, blocked):
    replies = db.session.query(PostReply).filter_by(user_id=blocked.id, deleted=False)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
            if reply.post.reply_count_cross_posted:  # as delete_post_or_comment does (D206)
                reply.post.reply_count_cross_posted -= 1
        reply.community.post_reply_count -= 1
        if reply.path and len(reply.path) > 1:
            db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                               {'parents': tuple(reply.path[:-1])})
    blocked.post_reply_count = 0
    db.session.commit()

    posts = db.session.query(Post).filter_by(user_id=blocked.id, deleted=False)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        adjust_domain_post_count(post, -1)  # D1362
        if post.url and post.cross_posts is not None:
            post.calculate_cross_posts(delete_only=True)
    blocked.post_count = 0
    db.session.commit()

    # Delete all their images to save moderators from having to see disgusting stuff.
    # Images attached to posts can't be restored, but site ban reversals don't have a 'removeData' field anyway.
    files = db.session.query(File).join(Post).filter(Post.user_id == blocked.id).all()
    for file in files:
        file.delete_from_disk(purge_cdn=True)
        file.source_url = ''
    if blocked.avatar_id:
        blocked.avatar.delete_from_disk()
        blocked.avatar.source_url = ''
    if blocked.cover_id:
        blocked.cover.delete_from_disk()
        blocked.cover.source_url = ''

    db.session.commit()


def community_ban_remove_data(blocker_id, community_id, blocked):
    replies = PostReply.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for reply in replies:
        reply.deleted = True
        reply.deleted_by = blocker_id
        if not blocked.bot:
            reply.post.reply_count -= 1
            if reply.post.reply_count_cross_posted:  # as delete_post_or_comment does (D206)
                reply.post.reply_count_cross_posted -= 1
        reply.community.post_reply_count -= 1
        blocked.post_reply_count -= 1
        if reply.path and len(reply.path) > 1:
            db.session.execute(text('update post_reply set child_count = child_count - 1 where id in :parents'),
                               {'parents': tuple(reply.path[:-1])})
    db.session.commit()

    posts = Post.query.filter_by(user_id=blocked.id, deleted=False, community_id=community_id)
    for post in posts:
        post.deleted = True
        post.deleted_by = blocker_id
        post.community.post_count -= 1
        adjust_domain_post_count(post, -1)  # D1362
        if post.url and post.cross_posts is not None:
            post.calculate_cross_posts(delete_only=True)
        blocked.post_count -= 1
    db.session.commit()

    # Delete attached images to save moderators from having to see disgusting stuff.
    files = File.query.join(Post).filter(Post.user_id == blocked.id, Post.community_id == community_id).all()
    for file in files:
        file.delete_from_disk()
        file.source_url = ''
    db.session.commit()


def parse_ban_expiry(core_activity):
    """Turn a peer-supplied ban expiry into a timezone-aware datetime.

    Returns None when the activity carries no expiry, when the value cannot
    be parsed, or when it has already passed. None is the right answer in
    all three cases: a NULL expiry means a permanent ban everywhere in this
    codebase (see User.banned_until's own comment in app/models.py).

    Malformed input is treated as "no expiry" rather than allowed to raise.
    process_inbox_request wraps every arm in `except Exception: rollback;
    raise` (app/activitypub/routes.py), so letting a bad date propagate
    would discard the entire ban, not just its expiry.
    """
    raw = core_activity.get('expires') or core_activity.get('endTime')
    if not raw:
        return None
    try:
        expires = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        # pendulum accepts shapes fromisoformat rejects, e.g. the ISO
        # ordinal date '2099-001'. It raises ParserError (a ValueError) on
        # garbage, and TypeError on a non-string.
        try:
            expires = pendulum.parse(raw)
        except Exception:
            return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires if expires > datetime.now(timezone.utc) else None


def ban_user(blocker, blocked, community, core_activity):
    if community is None:   # instance-wide ban
        target = core_activity['target']
        if 'summary' in core_activity:
            reason = core_activity['summary']
        else:
            reason = ''
        reason = shorten_string(reason, 255)

        instance_id = find_instance_id(furl(target).host)
        existing_ban = db.session.query(InstanceBan).filter(InstanceBan.user_id == blocked.id,
                                                            InstanceBan.instance_id == instance_id).first()
        # D205/D94 (owner ruling): a retransmitted Block for a ban already in force is a no-op - no second
        # notification or modlog entry - as in the community branch below
        if existing_ban:
            return
        instance_ban = InstanceBan(user_id=blocked.id, instance_id=instance_id)
        instance_ban.banned_until = parse_ban_expiry(core_activity)
        db.session.add(instance_ban)
        db.session.commit()

        if blocked.is_local():
            communities = instance_community_ids(instance_id)
            db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id.in_(communities),
                                                          CommunityJoinRequest.user_id == blocked.id).delete()

            # Notify banned person
            targets_data = {'gen': '0', 'instance_id': instance_id}
            notify = Notification(title=shorten_string('You have been banned from ' + target),
                                  url=f'/chat/ban_from_mod/{blocked.id}/{instance_id}', user_id=blocked.id,
                                  author_id=blocker.id, notif_type=NOTIF_BAN, subtype='user_banned_from_instance',
                                  targets=targets_data)
            db.session.add(notify)
            if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
                blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

            # Remove their notification subscription,  if any
            db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id.in_(communities),
                                                              NotificationSubscription.user_id == blocked.id,
                                                              NotificationSubscription.type == NOTIF_COMMUNITY).delete()
            db.session.commit()

            cache.delete_memoized(communities_banned_from, blocked.id)
            cache.delete_memoized(communities_banned_from_all_users)
            cache.delete_memoized(joined_communities, blocked.id)
            cache.delete_memoized(moderating_communities, blocked.id)
            cache.delete_memoized(banned_instances, blocked.id)
            cache.delete_memoized(blocked_or_banned_instances, blocked.id)

        add_to_modlog('ban_user', actor=blocker, target_user=blocked, reason=reason,
                      link_text=blocked.display_name(), link=f'u/{blocked.link()}')
    else:
        existing = CommunityBan.query.filter_by(community_id=community.id, user_id=blocked.id).first()
        if not existing:
            new_ban = CommunityBan(community_id=community.id, user_id=blocked.id, banned_by=blocker.id)
            if 'summary' in core_activity:
                reason = core_activity['summary']
            else:
                reason = ''
            new_ban.reason = shorten_string(reason, 255)

            new_ban.ban_until = parse_ban_expiry(core_activity)

            db.session.add(new_ban)

            community_membership_record = CommunityMember.query.filter_by(community_id=community.id,
                                                                          user_id=blocked.id).first()
            if community_membership_record:
                community_membership_record.is_banned = True
            db.session.commit()

            if blocked.is_local():
                db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == community.id,
                                                              CommunityJoinRequest.user_id == blocked.id).delete()

                # Notify banned person
                if community.has_poster(blocked):   # ... but only if they have posted in there before (mods can use bans to harass)
                    targets_data = {'gen': '0', 'community_id': community.id}
                    notify = Notification(title=shorten_string('You have been banned from ' + community.title),
                                          url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                                          author_id=blocker.id, notif_type=NOTIF_BAN, subtype='user_banned_from_community',
                                          targets=targets_data)
                    db.session.add(notify)
                    if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
                        blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

                # Remove their notification subscription,  if any
                db.session.query(NotificationSubscription).filter(NotificationSubscription.entity_id == community.id,
                                                                  NotificationSubscription.user_id == blocked.id,
                                                                  NotificationSubscription.type == NOTIF_COMMUNITY).delete()
                db.session.commit()

                cache.delete_memoized(communities_banned_from, blocked.id)
                cache.delete_memoized(communities_banned_from_all_users)
                cache.delete_memoized(joined_communities, blocked.id)
                cache.delete_memoized(moderating_communities, blocked.id)

            add_to_modlog('ban_user', actor=blocker, target_user=blocked, reason=reason,
                          community=community, link_text=blocked.display_name(), link=f'u/{blocked.link()}')


def unban_user(blocker, blocked, community, core_activity):
    if 'object' in core_activity and 'summary' in core_activity['object']:
        reason = core_activity['object']['summary']
    else:
        reason = ''
    if community is None:   # instance unban
        target = core_activity['object']['target']
        instance_id = find_instance_id(furl(target).host)
        db.session.query(InstanceBan).filter(InstanceBan.instance_id == instance_id, InstanceBan.user_id == blocked.id).delete()
        db.session.commit()
        if blocked.is_local():
            # Notify unbanned person
            targets_data = {'gen': '0', 'instance_id': instance_id}
            notify = Notification(title=shorten_string('You have been unbanned from ' + target),
                                  url=f'/chat/unban_from_mod/{blocked.id}/{instance_id}', user_id=blocked.id,
                                  author_id=blocker.id, notif_type=NOTIF_UNBAN,
                                  subtype='user_unbanned_from_instance',
                                  targets=targets_data)
            db.session.add(notify)
            if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
                blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

            db.session.commit()

            cache.delete_memoized(communities_banned_from, blocked.id)
            cache.delete_memoized(communities_banned_from_all_users)
            cache.delete_memoized(joined_communities, blocked.id)
            cache.delete_memoized(moderating_communities, blocked.id)
            cache.delete_memoized(banned_instances, blocked.id)
            cache.delete_memoized(blocked_or_banned_instances, blocked.id)

        add_to_modlog('unban_user', actor=blocker, target_user=blocked, reason=reason,
                      link_text=blocked.display_name(), link=f'u/{blocked.link()}')
    else:
        db.session.query(CommunityBan).filter(CommunityBan.community_id == community.id,
                                              CommunityBan.user_id == blocked.id).delete()
        community_membership_record = CommunityMember.query.filter_by(community_id=community.id, user_id=blocked.id).first()
        if community_membership_record:
            community_membership_record.is_banned = False
        db.session.commit()

        if blocked.is_local():
            # Notify unbanned person
            if community.has_poster(blocked):
                targets_data = {'gen': '0', 'community_id': community.id}
                notify = Notification(title=shorten_string('You have been unbanned from ' + community.display_name()),
                                      url=f'/chat/ban_from_mod/{blocked.id}/{community.id}', user_id=blocked.id,
                                      author_id=blocker.id, notif_type=NOTIF_UNBAN,
                                      subtype='user_unbanned_from_community',
                                      targets=targets_data)
                db.session.add(notify)
                if not current_app.debug:  # user.unread_notifications += 1 hangs app if 'user' is the same person
                    blocked.unread_notifications += 1  # who pressed 'Re-submit this activity'.

                db.session.commit()

            cache.delete_memoized(communities_banned_from, blocked.id)
            cache.delete_memoized(communities_banned_from_all_users)
            cache.delete_memoized(joined_communities, blocked.id)
            cache.delete_memoized(moderating_communities, blocked.id)

        add_to_modlog('unban_user', actor=blocker, target_user=blocked, reason=reason,
                      community=community, link_text=blocked.display_name(), link=f'u/{blocked.link()}')


def create_post_reply(store_ap_json, community: Community, in_reply_to, request_json: dict, user: User,
                      announce_id=None) -> Union[PostReply, None]:
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Community is local only, reply discarded')
        return None
    visibility = activitypub_visibility(request_json.get('object'))
    if visibility == 'direct':
        log_incoming_ap(id, APLOG_CREATE, APLOG_IGNORED, saved_json,
                        f'Non-public reply refused: {visibility}')
        return None
    # D1406, as create_post below: the object's id becomes `PostReply.ap_id`, which
    # app/templates/post/post_reply_options.html:194 renders as an `href`, and the
    # announced path checked nothing. Measured:
    #
    #     PROBE announced replies: [(1, 'javascript:alert(document.domain)')]
    #     PROBE announced log:     [('success', None)]
    if _as_url(request_json.get('object', {}).get('id')
               if isinstance(request_json.get('object'), dict) else None) is None:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json,
                        'Object id is not an http(s) url')
        return None
    post_id, parent_comment_id, root_id = find_reply_parent(in_reply_to)

    if post_id or parent_comment_id or root_id:
        # set depth to +1 of the parent depth
        if parent_comment_id:
            parent_comment = db.session.get(PostReply, parent_comment_id)
            if parent_comment.author.has_blocked_user(user.id) or parent_comment.author.has_blocked_instance(user.instance_id):
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Parent comment author blocked replier')
                return None
            if not parent_comment.replies_enabled:
                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Parent comment is locked')
                return None
        else:
            parent_comment = None
        if post_id is None:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Could not find parent post')
            return None
        post = db.session.get(Post, post_id)

        if post.archived:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Post is archived')
            return None

        if post.author.has_blocked_user(user.id) or post.author.has_blocked_instance(user.instance_id):
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Post author blocked replier')
            return None

        body = body_html = ''
        if 'content' in request_json['object'] and request_json['object']['content'] is not None:  # Kbin, Mastodon, etc provide their posts as html
            # A local, not request_json['object']['content']: that dict is the caller's (D139)
            content = request_json['object']['content']
            if not (content.startswith('<p>') or content.startswith('<blockquote>')):
                content = '<p>' + content + '</p>'
            body_html = allowlist_html(content)
            source_markdown = markdown_source(request_json['object'])  # D1346
            if source_markdown is not None:
                body = source_markdown
                body_html = markdown_to_html(body)  # prefer Markdown if provided, overwrite version obtained from HTML
            elif request_json['object'].get('mediaType') == 'text/markdown':
                # G2. PeerTube sends a comment as markdown `content` with no `source`
                body = request_json['object']['content']
                body_html = markdown_to_html(body)
            else:
                body = html_to_text(body_html)

        # Language - Lemmy uses 'language' while Mastodon uses 'contentMap'
        language_id = None
        ap_language = language_from_ap(request_json['object'].get('language'))  # D1355
        if ap_language is not None:
            language = find_language_or_create(*ap_language)
            if language.id is None:
                # A newly created Language is only add()ed, and under autoflush=False its id
                # stays None until a flush, so the reply was created with no language (D260).
                db.session.flush()
            language_id = language.id
        # A non-empty dict: an empty map names no language, like an absent one, and next(iter({})) raises (D272)
        elif isinstance(request_json['object'].get('contentMap'), dict) and request_json['object']['contentMap']:
            language = find_language(next(iter(request_json['object']['contentMap'])))  # Combination of next and iter gets the first key in a dict
            language_id = language.id if language else None
        else:
            language_id = site_language_id()

        distinguished = request_json['object']['distinguished'] if 'distinguished' in request_json['object'] else False
        #answer = request_json['object']['answer'] if 'answer' in request_json['object'] else False

        if 'attachment' in request_json['object']:
            attachment_list = []
            if isinstance(request_json['object']['attachment'], dict):
                attachment_list.append(request_json['object']['attachment'])
            elif isinstance(request_json['object']['attachment'], list):
                attachment_list = request_json['object']['attachment']
            for attachment in attachment_list:
                # D1397. A peer's `attachment` array may hold a bare url string,
                # and `'href' in <string>` is a substring test whose subscript is
                # a TypeError.
                attachment = _as_dict(attachment)
                url = alt_text = ''
                if 'href' in attachment:
                    url = attachment['href']
                if 'url' in attachment:
                    url = attachment['url']
                if 'name' in attachment:
                    alt_text = attachment['name']
                if url:
                    body = body + f"\n\n![{alt_text}]({url})"
            if attachment_list:
                body_html = markdown_to_html(body)

        # Check for Mentions of local users
        reply_parent = parent_comment if parent_comment else post
        local_users_to_notify = []
        if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
            for json_tag in request_json['object']['tag']:
                # D1397. `'type' in json_tag` over a STRING element is a substring
                # test, and `json_tag['type']` then raises -- one `tag` entry of
                # `"#prototype"` stopped the whole activity.
                json_tag = _as_dict(json_tag)
                if 'type' in json_tag and json_tag['type'] == 'Mention':
                    profile_id = json_tag['href'] if 'href' in json_tag else None
                    if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                        profile_id = profile_id.lower()
                        # once per recipient: a repeated Mention tag notified twice (D262)
                        if profile_id != reply_parent.author.ap_profile_id and profile_id not in local_users_to_notify:
                            local_users_to_notify.append(profile_id)

        try:
            post_reply = PostReply.new(user, post, parent_comment, notify_author=False, body=body, body_html=body_html,
                                       language_id=language_id, distinguished=distinguished, answer=False, request_json=request_json,
                                       announce_id=announce_id)
            # The object's own repliesEnabled, stored as update_post_reply_from_activity does (D269)
            if 'repliesEnabled' in request_json['object']:
                post_reply.replies_enabled = request_json['object']['repliesEnabled']
                db.session.commit()
            # The replier's flair, applied only once the reply is accepted: a refused reply changes nothing.
            # A non-string flair is skipped, and both branches strip (D273)
            if isinstance(request_json['object'].get('flair'), str) and request_json['object']['flair'].strip():
                existing_flair = UserFlair.query.filter(UserFlair.user_id == user.id,
                                                        UserFlair.community_id == community.id).first()
                if existing_flair:
                    existing_flair.flair = request_json['object']['flair'].strip()
                else:
                    db.session.add(UserFlair(user_id=user.id, community_id=community.id,
                                             flair=request_json['object']['flair'].strip()))
                db.session.commit()
            for lutn in local_users_to_notify:
                recipient = db.session.query(User).filter_by(ap_profile_id=lutn, ap_id=None).first()
                if not recipient or not can_view(post_reply, recipient.id):
                    continue
                if post_reply.instance.software == 'mbin' or post_reply.instance.software in MICROBLOG_APPS:
                    # ignore Mention of post author from microblog apps
                    # (direct replies will generate a different kind of Notification)
                    if recipient.id == post.user_id:
                        continue

                    # ignore Mentions in comments from MBIN if they're just mirroring a Mention made in a post body
                    notifs = db.session.query(Notification).filter(Notification.user_id == recipient.id,
                                                                   Notification.notif_type == NOTIF_MENTION,
                                                                   Notification.subtype == "post_mention",
                                                                   Notification.targets.op("->>")("post_id").cast(Integer) == post_reply.post_id).first()
                    if notifs:
                        continue

                    # ignore Mentions in comments from MBIN if they're just mirroring a Mention someone else made in the comment chain
                    ids = []
                    for element in post_reply.path:
                        if element == 0 or element == post_reply.id:
                            continue
                        ids.append(element)
                    notifs = db.session.query(Notification).filter(Notification.user_id == recipient.id,
                                                                   Notification.notif_type == NOTIF_MENTION,
                                                                   Notification.subtype == "comment_mention",
                                                                   Notification.targets.op("->>")("comment_id").cast(Integer).in_(ids)).first()
                    if notifs:
                        continue

                    # ignore Mentions in comments from MBIN if they're just there because a local user authored a comment further up in the comment chain
                    # (direct replies will generate a different kind of Notification)
                    # note: can't just check for any Notifications (reply or mention) 'cos local users could conceivably have turned inbox replies off
                    # a top-level comment has no ancestors, and psycopg2 renders an empty tuple as an
                    # invalid `IN ()`, so skip the lookup rather than ask it about nobody
                    ids = tuple(ids)
                    if ids:
                        user_ids = db.session.execute(text('SELECT user_id FROM "post_reply" WHERE id IN :ids'), {'ids': ids}).scalars()
                        if recipient.id in user_ids:
                            continue

                    # if checking for previous comments seems overly-involved, a cheaper solution is perhaps to just be to reject Mentions from MBIN in comments with a depth > 0

                blocked_senders = blocked_users(recipient.id)
                if post_reply.user_id not in blocked_senders:
                    author = db.session.get(User, post_reply.user_id)
                    targets_data = {'gen': '0',
                                    'post_id': post_reply.post_id,
                                    'comment_id': post_reply.id,
                                    'comment_body': post_reply.body,
                                    'author_user_name': author.ap_id if author.ap_id else author.user_name
                                    }
                    with force_locale(get_recipient_language(recipient.id)):
                        notification = Notification(user_id=recipient.id, title=gettext(
                            f"You have been mentioned in comment {post_reply.id}"),
                                                    url=f"{current_app.config['SERVER_URL']}/comment/{post_reply.id}",
                                                    author_id=user.id, notif_type=NOTIF_MENTION,
                                                    subtype='comment_mention',
                                                    targets=targets_data)
                        recipient.unread_notifications += 1
                        db.session.add(notification)
                        db.session.commit()

            return post_reply
        except PostReplyValidationError as ex:  # PostReply.new's refusals, this function's normal refusal path
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
            return None
        except Exception as ex:
            # D263. Anything else is a real failure, not a refusal: roll back first so the log row is not written
            # into a transaction the error has aborted, then re-raise rather than return a None that looks like one.
            db.session.rollback()
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
            raise
    else:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unable to find parent post/comment')
        return None


def create_post(store_ap_json, community: Community, request_json: dict, user: User, announce_id=None) -> Union[Post, None]:
    saved_json = request_json if store_ap_json else None
    id = request_json['id']
    if community.local_only:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Community is local only, post discarded')
        return None
    visibility = activitypub_visibility(request_json.get('object'))
    if visibility == 'direct':
        log_incoming_ap(id, APLOG_CREATE, APLOG_IGNORED, saved_json,
                        f'Non-public post refused: {visibility}')
        return None
    # D1406. `request_json['object']['id']` becomes `Post.ap_id`, which is a LOOKUP
    # KEY (`Post.get_by_ap_id`), a FETCH TARGET (`resolve_remote_post`) and an
    # `href`: app/templates/post/post_options.html:234 renders it as "view on
    # remote instance". Nothing on the ANNOUNCED path checked it --
    # `ensure_domains_match` sits inside `if not announced and not community:`
    # (app/activitypub/routes.py:1246), which an Announce skips, and an Announce is
    # the ordinary way a Lemmy community relays a post. Measured, through the
    # dispatcher:
    #
    #     PROBE announced posts: [(1, 'javascript:alert(document.domain)', 'A post')]
    #     PROBE announced log:   [('success', None)]
    #
    # An ActivityPub id is an https URL by specification, and this instance signs
    # requests to it, so http(s) is an allowlist with nothing to audit -- the same
    # argument as an image url (D1405) rather than a link a person clicks (D1404).
    # Refused rather than dropped, because ap_id is not an optional field: a post
    # with no id cannot be deduplicated, updated or deleted by its author later.
    if _as_url(request_json.get('object', {}).get('id')
               if isinstance(request_json.get('object'), dict) else None) is None:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json,
                        'Object id is not an http(s) url')
        return None
    try:
        post = Post.new(user, community, request_json, announce_id)
        return post
    except Exception as ex:
        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
        return None


def notify_about_post(post: Post):
    if current_app.debug:
        notify_about_post_task(post.id)
    else:
        notify_about_post_task.delay(post.id)


@celery.task
def notify_about_post_task(post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            # get the post by id
            post = session.get(Post, post_id)

            # get the author
            author = session.get(User, post.user_id)

            # get the community
            community = session.get(Community, post.community_id)

            # Send notifications based on subscriptions. A retried task skips anyone an earlier
            # attempt already notified about this post, as the fan-out commits per recipient (D278)
            notifications_sent_to = {row.user_id for row in session.query(Notification.user_id).filter(
                Notification.url == f"/post/{post.id}")}

            # NOTIF_USER 
            user_send_notifs_to = notification_subscribers(post.user_id, NOTIF_USER)
            for notify_id in user_send_notifs_to:
                blocked_senders = blocked_users(notify_id)  # D276
                blocked_comms = blocked_communities(notify_id)
                blocked_ints = blocked_or_banned_instances(notify_id)
                if notify_id != post.user_id and notify_id not in notifications_sent_to and \
                        post.user_id not in blocked_senders and \
                        post.community_id not in blocked_comms and \
                        post.instance_id not in blocked_ints and \
                        can_view(post, notify_id):
                    targets_data = {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': post.title,
                                    'community_name': community.ap_id if community.ap_id else community.name,
                                    'author_id': post.user_id,
                                    'author_user_name': author.ap_id if author.ap_id else author.user_name}
                    new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                    user_id=notify_id, author_id=post.user_id,
                                                    notif_type=NOTIF_USER,
                                                    subtype='new_post_from_followed_user',
                                                    targets=targets_data)
                    with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):  # D279
                        session.add(new_notification)
                        user = session.get(User, notify_id)
                        user.unread_notifications += 1
                        session.commit()
                    notifications_sent_to.add(notify_id)

            # NOTIF_COMMUNITY
            community_send_notifs_to = notification_subscribers(post.community_id, NOTIF_COMMUNITY)
            for notify_id in community_send_notifs_to:
                blocked_senders = blocked_users(notify_id)
                blocked_comms = blocked_communities(notify_id)  # D277
                blocked_ints = blocked_or_banned_instances(notify_id)
                if notify_id != post.user_id and notify_id not in notifications_sent_to and \
                        post.user_id not in blocked_senders and post.community_id not in blocked_comms and \
                        post.instance_id not in blocked_ints and \
                        can_view(post, notify_id):
                    targets_data = {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': post.title,
                                    'community_name': community.ap_id if community.ap_id else community.name,
                                    'community_id': post.community_id}
                    new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                    user_id=notify_id, author_id=post.user_id,
                                                    notif_type=NOTIF_COMMUNITY,
                                                    subtype='new_post_in_followed_community',
                                                    targets=targets_data)
                    with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):  # D279
                        session.add(new_notification)
                        user = session.get(User, notify_id)
                        user.unread_notifications += 1
                        session.commit()
                    notifications_sent_to.add(notify_id)

            # NOTIF_TOPIC    
            topic_send_notifs_to = notification_subscribers(post.community.topic_id, NOTIF_TOPIC)
            if post.community.topic_id:
                topic = session.get(Topic, post.community.topic_id)
            for notify_id in topic_send_notifs_to:
                blocked_senders = blocked_users(notify_id)
                blocked_comms = blocked_communities(notify_id)
                blocked_ints = blocked_or_banned_instances(notify_id)
                if notify_id != post.user_id and \
                        notify_id not in notifications_sent_to and \
                        post.user_id not in blocked_senders and \
                        post.community_id not in blocked_comms and \
                        post.instance_id not in blocked_ints and \
                        can_view(post, notify_id):
                    targets_data = {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': post.title,
                                    'community_name': community.ap_id if community.ap_id else community.name,
                                    'topic_name': topic.name,
                                    'topic_machine_name': topic.machine_name,
                                    'author_id': post.user_id}
                    new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                    user_id=notify_id, author_id=post.user_id,
                                                    notif_type=NOTIF_TOPIC,
                                                    subtype='new_post_in_followed_topic',
                                                    targets=targets_data)
                    with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):  # D279
                        session.add(new_notification)
                        user = session.get(User, notify_id)
                        user.unread_notifications += 1
                        session.commit()
                    notifications_sent_to.add(notify_id)

            # NOTIF_FEED
            # Get all the feeds that the post's community is in
            community_feeds = session.query(Feed).join(FeedItem, FeedItem.feed_id == Feed.id).filter(
                FeedItem.community_id == post.community_id).all()

            for feed in community_feeds:
                feed_send_notifs_to = notification_subscribers(feed.id, NOTIF_FEED)
                for notify_id in feed_send_notifs_to:
                    blocked_senders = blocked_users(notify_id)
                    blocked_comms = blocked_communities(notify_id)
                    blocked_ints = blocked_or_banned_instances(notify_id)
                    if notify_id != post.user_id and \
                            notify_id not in notifications_sent_to and \
                            post.user_id not in blocked_senders and \
                            post.community_id not in blocked_comms and \
                            post.instance_id not in blocked_ints and \
                            can_view(post, notify_id):
                        targets_data = {'gen': '0',
                                        'post_id': post.id,
                                        'post_title': post.title,
                                        'community_name': community.ap_id if community.ap_id else community.name,
                                        'feed_id': feed.id,
                                        'feed_name': feed.title
                                        }
                        new_notification = Notification(title=shorten_string(post.title, 150), url=f"/post/{post.id}",
                                                        user_id=notify_id, author_id=post.user_id,
                                                        notif_type=NOTIF_FEED,
                                                        subtype='new_post_in_followed_feed',
                                                        targets=targets_data)
                        with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):  # D279
                            session.add(new_notification)
                            user = session.get(User, notify_id)
                            user.unread_notifications += 1
                            session.commit()
                        notifications_sent_to.add(notify_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def notify_about_post_reply(parent_reply: Union[PostReply, None], new_reply: PostReply):
    if parent_reply is None:  # This happens when a new_reply is a top-level comment, not a comment on a comment
        send_notifs_to = notification_subscribers(new_reply.post.id, NOTIF_POST)
        post = db.session.get(Post, new_reply.post.id)
        community = db.session.get(Community, post.community_id)
        author = db.session.get(User, new_reply.user_id)
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id and can_view(new_reply, notify_id):
                targets_data = {'gen': '0',
                                'post_id': new_reply.post.id,
                                'post_title': post.title,
                                'community_name': community.ap_id if community.ap_id else community.name,
                                'author_user_name': author.ap_id if author.ap_id else author.user_name,
                                'comment_id': new_reply.id,
                                'comment_body': new_reply.body}
                new_notification = Notification(title=shorten_string(_('Reply to %(post_title)s',
                                                                       post_title=new_reply.post.title), 150),
                                                url=f"/post/{new_reply.post.id}/comment/{new_reply.id}#comment_{new_reply.id}",
                                                user_id=notify_id, author_id=new_reply.user_id,
                                                notif_type=NOTIF_POST,
                                                subtype='top_level_comment_on_followed_post',
                                                targets=targets_data)
                with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):
                    db.session.add(new_notification)
                    user = db.session.get(User, notify_id)
                    user.unread_notifications += 1
                    db.session.commit()
    else:
        # Set notifications about parent_reply to read
        db.session.execute(
            update(Notification)
            .where(Notification.user_id == new_reply.user_id)
            .where(Notification.targets['comment_id'].as_string() == str(parent_reply.id))
            .values(read=True)
        )
        db.session.commit()

        with app_pkg.redis_client.lock(f"lock:user:{new_reply.user_id}", timeout=10, blocking_timeout=6):
            user = db.session.get(User, new_reply.user_id)
            user.unread_notifications = Notification.query.filter_by(user_id=user.id, read=False).count()
            db.session.commit()

        # Send notifications based on subscriptions
        send_notifs_to = set(notification_subscribers(parent_reply.id, NOTIF_REPLY))
        for notify_id in send_notifs_to:
            if new_reply.user_id != notify_id and can_view(new_reply, notify_id):
                author = db.session.get(User, new_reply.user_id)
                targets_data = {'gen': '0',
                                'post_id': parent_reply.post.id,
                                'parent_comment_id': new_reply.parent_id,
                                'parent_reply_body': parent_reply.body,
                                'comment_id': new_reply.id,
                                'comment_body': new_reply.body,
                                'author_id': new_reply.user_id,
                                'author_user_name': author.ap_id if author.ap_id else author.user_name, }
                with force_locale(get_recipient_language(notify_id)):
                    new_notification = Notification(
                        title=shorten_string(gettext('Reply to comment on %(post_title)s',
                                                     post_title=parent_reply.post.title), 150),
                        url=f"/post/{parent_reply.post.id}/comment/{new_reply.parent_id}#comment_{new_reply.id}",
                        user_id=notify_id, author_id=new_reply.user_id,
                        notif_type=NOTIF_REPLY,
                        subtype='new_reply_on_followed_comment',
                        targets=targets_data)
                db.session.add(new_notification)
                with app_pkg.redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6):
                    user = db.session.get(User, notify_id)
                    user.unread_notifications += 1
                    db.session.commit()


def update_post_reply_from_activity(reply: PostReply, request_json: dict):
    # the same lock, content and language rules as update_post_from_activity (D249, D250, D252, D253)
    with app_pkg.redis_client.lock(f"lock:post_reply:{reply.id}", timeout=60, blocking_timeout=60):
        if 'content' in request_json['object'] and request_json['object']['content'] is not None:   # Kbin, Mastodon, etc provide their posts as html
            # prefer Markdown in 'source' in provided
            source_markdown = markdown_source(request_json['object'])  # D1346
            if source_markdown is not None:
                reply.body = source_markdown
                reply.body_html = markdown_to_html(reply.body)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
                reply.body_html = allowlist_html(request_json['object']['content'])
                reply.body = html_to_text(reply.body_html)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
                reply.body = request_json['object']['content']
                reply.body_html = markdown_to_html(reply.body)
            else:
                # A local, not request_json['object']['content']: that dict is the caller's (D139)
                content = request_json['object']['content']
                if not (content.startswith('<p>') or content.startswith('<blockquote>')):
                    content = '<p>' + content + '</p>'
                reply.body_html = allowlist_html(content)
                reply.body = html_to_text(reply.body_html)
        reply.content_warning = content_warning_from(request_json['object'])
        # Language
        old_language_id = reply.language_id
        new_language = None
        ap_language = language_from_ap(request_json['object'].get('language'))  # D1355
        if ap_language is not None:
            new_language = find_language_or_create(*ap_language)
        # A non-empty dict: an empty map names no language, like an absent one, and next(iter({})) raises (D255)
        elif isinstance(request_json['object'].get('contentMap'), dict) and request_json['object']['contentMap']:
            new_language = find_language(next(iter(request_json['object']['contentMap'])))
        # find_language_or_create() can return a row it has only add()ed, whose id is
        # still None (the app factory sets autoflush=False). Assign the relationship
        # and let SQLAlchemy resolve the id at flush, as the tag and flair arms do.
        if new_language and (new_language.id is None or new_language.id != old_language_id):
            reply.language = new_language

        # Distinguished
        if 'distinguished' in request_json['object']:
            reply.distinguished = request_json['object']['distinguished']

        if 'repliesEnabled' in request_json['object']:
            reply.replies_enabled = request_json['object']['repliesEnabled']

        reply.edited_at = utcnow()

        if 'attachment' in request_json['object']:
            attachment_list = []
            if isinstance(request_json['object']['attachment'], dict):
                attachment_list.append(request_json['object']['attachment'])
            elif isinstance(request_json['object']['attachment'], list):
                attachment_list = request_json['object']['attachment']
            for attachment in attachment_list:
                # D1397. A peer's `attachment` array may hold a bare url string,
                # and `'href' in <string>` is a substring test whose subscript is
                # a TypeError.
                attachment = _as_dict(attachment)
                url = alt_text = ''
                if 'href' in attachment:
                    url = attachment['href']
                if 'url' in attachment:
                    url = attachment['url']
                if 'name' in attachment:
                    alt_text = attachment['name']
                if url:
                    reply.body = reply.body + f"\n\n![{alt_text}]({url})"
            if attachment_list:
                reply.body_html = markdown_to_html(reply.body)

        try:
            reply.ap_updated = datetime.fromisoformat(request_json['object']['updated']) if 'updated' in request_json['object'] else utcnow()
        except (ValueError, TypeError):
            reply.ap_updated = utcnow()

        # Check for Mentions of local users (that weren't in the original)
        if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
            # under autoflush=False the query below cannot see this call's own pending
            # notifications, so a repeated Mention tag notified twice (D242)
            notified_ids = set()
            for json_tag in request_json['object']['tag']:
                # D1397. `'type' in json_tag` over a STRING element is a substring
                # test, and `json_tag['type']` then raises -- one `tag` entry of
                # `"#prototype"` stopped the whole activity.
                json_tag = _as_dict(json_tag)
                if 'type' in json_tag and json_tag['type'] == 'Mention':
                    profile_id = json_tag['href'] if 'href' in json_tag else None
                    if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                        profile_id = profile_id.lower()
                        if reply.parent_id:
                            reply_parent = db.session.get(PostReply, reply.parent_id)
                        else:
                            reply_parent = reply.post
                        if reply_parent and profile_id != reply_parent.author.ap_profile_id:
                            recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                            if recipient and can_view(reply, recipient.id):
                                if reply.instance.software == 'mbin' or reply.instance.software in MICROBLOG_APPS:
                                    # ignore Mention of post author
                                    if recipient.id == reply.post.user_id:
                                        continue

                                    # ignore Mentions mirroring a Mention made in a post body
                                    notifs = db.session.query(Notification).filter(Notification.user_id == recipient.id,
                                                                                   Notification.notif_type == NOTIF_MENTION,
                                                                                   Notification.subtype == "post_mention",
                                                                                   Notification.targets.op("->>")("post_id").cast(Integer) == reply.post_id).first()
                                    if notifs:
                                        continue

                                    # ignore Mentions mirroring a Mention someone else made in the comment chain
                                    ids = []
                                    for element in reply.path:
                                        if element == 0 or element == reply.id:
                                            continue
                                        ids.append(element)
                                    notifs = db.session.query(Notification).filter(Notification.user_id == recipient.id,
                                                                                   Notification.notif_type == NOTIF_MENTION,
                                                                                   Notification.subtype == "comment_mention",
                                                                                   Notification.targets.op("->>")("comment_id").cast(Integer).in_(ids)).first()
                                    if notifs:
                                        continue

                                    # ignore Mentions generated because a local user authored a comment further up in the comment chain
                                    # a top-level comment has no ancestors, and psycopg2 renders an empty tuple as an
                                    # invalid `IN ()`, so skip the lookup rather than ask it about nobody
                                    ids = tuple(ids)
                                    if ids:
                                        user_ids = db.session.execute(text('SELECT user_id FROM "post_reply" WHERE id IN :ids'), {'ids': ids}).scalars()
                                        if recipient.id in user_ids:
                                            continue

                                blocked_senders = blocked_users(recipient.id)
                                if reply.user_id not in blocked_senders:
                                    existing_notification = Notification.query.filter(Notification.user_id == recipient.id,
                                                                                      Notification.url == f"{current_app.config['SERVER_URL']}/comment/{reply.id}").first()
                                    if not existing_notification and recipient.id not in notified_ids:
                                        notified_ids.add(recipient.id)
                                        author = db.session.get(User, reply.user_id)
                                        targets_data = {'gen': '0',
                                                        'post_id': reply.post_id,
                                                        'comment_id': reply.id,
                                                        'comment_body': reply.body,
                                                        'author_user_name': author.ap_id if author.ap_id else author.user_name
                                                        }
                                        with force_locale(get_recipient_language(recipient.id)):
                                            notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in comment {reply.id}"),
                                                                        url=f"{current_app.config['SERVER_URL']}/comment/{reply.id}",
                                                                        author_id=reply.user_id, notif_type=NOTIF_MENTION,
                                                                        subtype='comment_mention',
                                                                        targets=targets_data)
                                            recipient.unread_notifications += 1
                                            db.session.add(notification)

        db.session.commit()


def _is_vote_count(value) -> bool:
    """A poll choice's totalItems as a peer should send it: a non-negative int (bool excluded) (D291)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def update_post_from_activity(post: Post, request_json: dict):
    with app_pkg.redis_client.lock(f"lock:post:{post.id}", timeout=60, blocking_timeout=60):
        # redo body without checking if it's changed
        if 'content' in request_json['object'] and request_json['object']['content'] is not None:
            # prefer Markdown in 'source' in provided
            source_markdown = markdown_source(request_json['object'])  # D1346
            if source_markdown is not None:
                post.body = source_markdown
                post.body_html = markdown_to_html(post.body)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/html':
                post.body_html = allowlist_html(request_json['object']['content'])
                post.body = html_to_text(post.body_html)
            elif 'mediaType' in request_json['object'] and request_json['object']['mediaType'] == 'text/markdown':
                post.body = request_json['object']['content']
                post.body_html = markdown_to_html(post.body)
            else:
                # A local, not request_json['object']['content']: that dict is the caller's (D139)
                content = request_json['object']['content']
                if not (content.startswith('<p>') or content.startswith('<blockquote>')):
                    content = '<p>' + content + '</p>'
                post.body_html = allowlist_html(content)
                post.body = html_to_text(post.body_html)

        # title
        old_title = post.title
        # A null name means no title, like an absent one: None.upper() below would raise (D258)
        if 'name' in request_json['object'] and request_json['object']['name'] is not None:
            new_title = request_json['object']['name']
            post.microblog = False
        else:
            autogenerated_title, link = microblog_content_to_title(post.body_html)
            if len(autogenerated_title) < 20:
                new_title = '[Microblog] ' + autogenerated_title.strip()
            else:
                new_title = autogenerated_title.strip()
            if link != '':
                # `link` is the href of an anchor in a REMOTE peer's content.
                # It has already been through allowlist_html, which blanks an
                # unsafe scheme, so the scheme half of url_is_storable is
                # defence in depth here rather than the live hole it closes at
                # the attachment sites (D1404). One predicate at all four.
                # Store nothing rather than an unparseable string: the render
                # path re-parses post.url, and refusing the whole Update over
                # it would hand peers a way to make us drop their content.
                # None, not '', because None is what Post.url holds for every
                # post that has no url, and post_to_page (this file, ~line 170)
                # is the one consumer that can tell None from '': it gates the
                # outbound attachment on `post.url is not None`, so '' would
                # federate {"href": ""} back out.
                post.url = link if url_is_storable(link) else None
            post.microblog = True
            if warning := content_warning_from(request_json['object']):
                new_title = shorten_string(warning, 255)  # a warning hides the body, so the body must not become the title

        if old_title != new_title:
            post.title = new_title
            if '[NSFL]' in new_title.upper() or '(NSFL)' in new_title.upper() or '[COMBAT]' in new_title.upper():
                post.nsfl = True
            if '[NSFW]' in new_title.upper() or '(NSFW)' in new_title.upper():
                post.nsfw = True
        if 'sensitive' in request_json['object']:
            post.nsfw = request_json['object']['sensitive']
        post.content_warning = content_warning_from(request_json['object'])
        if 'nsfl' in request_json['object']:
            post.nsfl = request_json['object']['nsfl']

        # Language
        old_language_id = post.language_id
        new_language = None
        ap_language = language_from_ap(request_json['object'].get('language'))  # D1355
        if ap_language is not None:
            new_language = find_language_or_create(*ap_language)
        # A non-empty dict: an empty map names no language, like an absent one, and next(iter({})) raises (D255)
        elif isinstance(request_json['object'].get('contentMap'), dict) and request_json['object']['contentMap']:
            new_language = find_language(next(iter(request_json['object']['contentMap'])))
        # find_language_or_create() can return a row it has only add()ed, whose id is still
        # None (the app factory sets autoflush=False), so `id is None` means "brand new" and
        # not "same as a post that has no language". Assign the relationship and let
        # SQLAlchemy resolve the id at flush, as the tag and flair arms do.
        if new_language and (new_language.id is None or new_language.id != old_language_id):
            post.language = new_language

        # Tags
        if 'tag' in request_json['object'] and isinstance(request_json['object']['tag'], list):
            post.tags.clear()
            # change back when lemmy supports flairs
            # post.flair.clear()
            flair_tags = []
            notified_ids = set()  # a repeated Mention tag notifies once (D242), as in the reply update
            for json_tag in request_json['object']['tag']:
                # D1397, as the two reply tag loops.
                json_tag = _as_dict(json_tag)
                # a Hashtag with no name has nothing to attach, so skip it rather than raise after tags were cleared
                if 'type' in json_tag and json_tag['type'] == 'Hashtag' and isinstance(json_tag.get('name'), str):
                    if json_tag['name'][
                       1:].lower() != post.community.name.lower():  # Lemmy adds the community slug as a hashtag on every post in the community, which we want to ignore
                        hashtag = find_hashtag_or_create(json_tag['name'])
                        if hashtag:
                            post.tags.append(hashtag)
                if 'type' in json_tag and json_tag['type'] == 'lemmy:CommunityTag':
                    # change back when lemmy supports flairs
                    # flair = find_flair_or_create(json_tag, post.community_id)
                    # if flair:
                    #    post.flair.append(flair)
                    flair_tags.append(json_tag)
                if 'type' in json_tag and json_tag['type'] == 'Mention':
                    profile_id = json_tag['href'] if 'href' in json_tag else None
                    if profile_id and isinstance(profile_id, str) and profile_id.startswith('https://' + current_app.config['SERVER_NAME']):
                        profile_id = profile_id.lower()
                        recipient = User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()
                        # the reply path's suppression rules that mean anything on a post: the author
                        # mentioning themselves, a block, and a notification already sent (D254)
                        if recipient and recipient.id != post.user_id and can_view(post, recipient.id):
                            blocked_senders = blocked_users(recipient.id)
                            if post.user_id not in blocked_senders:
                                existing_notification = Notification.query.filter(Notification.user_id == recipient.id,
                                                                                  Notification.url == f"{current_app.config['SERVER_URL']}/post/{post.id}").first()
                                if not existing_notification and recipient.id not in notified_ids:
                                    notified_ids.add(recipient.id)
                                    author = db.session.get(User, post.user_id)
                                    targets_data = {'gen': '0',
                                                    'post_id': post.id,
                                                    'post_title': post.title,
                                                    'post_body': post.body,
                                                    'author_user_name': author.ap_id if author.ap_id else author.user_name
                                                    }
                                    # in the recipient's language, as the reply path does (D248)
                                    with force_locale(get_recipient_language(recipient.id)):
                                        notification = Notification(user_id=recipient.id,
                                                                    title=gettext(f"You have been mentioned in post {post.id}"),
                                                                    url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                                    author_id=post.user_id, notif_type=NOTIF_MENTION,
                                                                    subtype='post_mention',
                                                                    targets=targets_data)
                                    recipient.unread_notifications += 1
                                    db.session.add(notification)
            # remove when lemmy supports flairs
            # for now only clear tags if there's new ones or if maybe another PieFed instance is trying to remove them
            if len(flair_tags) > 0 or (post.instance.software == 'piefed' or post.instance.software == 'pylova'):
                post.flair.clear()
                for ft in flair_tags:
                    flair = find_flair_or_create(ft, post.community_id)
                    if flair:
                        post.flair.append(flair)

        post.comments_enabled = request_json['object']['commentsEnabled'] if 'commentsEnabled' in request_json['object'] else True
        try:
            post.ap_updated = datetime.fromisoformat(request_json['object']['updated']) if 'updated' in request_json['object'] else utcnow()
        except (ValueError, TypeError):
            post.ap_updated = utcnow()
        post.edited_at = utcnow()

        # D1397. `request_json['object']['type']` -- a peer key read with no
        # membership test, and the inbox's own `object_has_missing_fields` checks
        # the ACTIVITY's keys, not the object's. An Update whose object carries no
        # `type` was a KeyError here, after the function had already written the
        # title, the tags and the flair.
        if request_json['object'].get('type') == 'Video':
            # fetching individual user details to attach to votes is probably too convoluted, so take the instance's word for it
            upvotes = 1  # from OP
            downvotes = 0
            endpoints = ['likes', 'dislikes']
            for endpoint in endpoints:
                if endpoint in request_json['object']:
                    try:
                        object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:
                        time.sleep(3)
                        try:
                            object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                        except httpx.HTTPError:
                            object_request = None
                    if object_request and object_request.status_code == 200:
                        try:
                            object = object_request.json()
                        except:
                            object_request.close()
                            object = None
                        object_request.close()
                        if object and 'totalItems' in object:
                            if endpoint == 'likes':
                                upvotes += object['totalItems']
                            if endpoint == 'dislikes':
                                downvotes += object['totalItems']

            multiplier = 1.0
            post.up_votes = upvotes * multiplier
            post.down_votes = downvotes
            post.score = upvotes - downvotes
            post.ranking = post.post_ranking(post.score + post.reply_count, post.posted_at)
            post.ranking_scaled = int(post.ranking + post.community.scale_by())
            # return now for PeerTube, otherwise rest of this function breaks the post
            db.session.commit()
            return

        if request_json['object'].get('type') == 'Question':  # D1397, as :3495
            # an Update is probably just informing us of new totals, but it could be an Edit to the Poll itself (totalItems for all choices will be 0)
            mode = 'single'
            if 'oneOf' in request_json['object']:
                votes = request_json['object']['oneOf']
            elif 'anyOf' in request_json['object']:
                votes = request_json['object']['anyOf']
                mode = 'multiple'
            else:
                return

            total_vote_count = 0
            for vote in votes:
                # D1397. `vote` and `vote['replies']` are both peer values and
                # neither was checked: a string element made `'name' in vote` a
                # substring test, and `vote['replies']` a TypeError.
                vote = _as_dict(vote)
                if not 'name' in vote:
                    continue
                if not 'replies' in vote:
                    continue
                if not 'totalItems' in _as_dict(vote['replies']):
                    continue
                # D291. A count is a non-negative int, and anything else is treated as no count, like a missing
                # totalItems: a negative could cancel another to the 0 that routes into the destructive Edit path
                # below, and a non-number raised TypeError here.
                if not _is_vote_count(vote['replies']['totalItems']):
                    continue

                total_vote_count += vote['replies']['totalItems']

            if total_vote_count == 0:  # Edit, not a totals update
                poll = Poll.query.filter_by(post_id=post.id).first()
                if poll:
                    # D1330's other half. The membership test was here already;
                    # the string still went straight into a DateTime column, so a
                    # peer sending `endTime: "not a date"` was a DataError that
                    # took the whole edit with it.
                    end_poll = parse_ap_timestamp(request_json['object'].get('endTime'))
                    if end_poll is None:
                        return
                    poll.end_poll = end_poll
                    poll.mode = mode

                    db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'),
                                       {'post_id': post.id})
                    db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

                    i = 1
                    for vote in votes:
                        # D1397, as the two totals loops around this one.
                        vote = _as_dict(vote)
                        if not 'name' in vote:
                            continue
                        new_choice = PollChoice(post_id=post.id, choice_text=vote['name'], sort_order=i)
                        db.session.add(new_choice)
                        i += 1
                    db.session.commit()
                return

            # totals Update
            for vote in votes:
                # D1397. `vote` and `vote['replies']` are both peer values and
                # neither was checked: a string element made `'name' in vote` a
                # substring test, and `vote['replies']` a TypeError.
                vote = _as_dict(vote)
                if not 'name' in vote:
                    continue
                if not 'replies' in vote:
                    continue
                if not 'totalItems' in _as_dict(vote['replies']):
                    continue
                if not _is_vote_count(vote['replies']['totalItems']):  # D291, as the counting loop
                    continue
                choice = PollChoice.query.filter_by(post_id=post.id, choice_text=vote['name']).first()
                if choice:
                    choice.num_votes = vote['replies']['totalItems']
            db.session.commit()
            # no URLs in Polls to worry about, so return now
            return

        old_db_entry_to_delete = None

        if request_json['object'].get('type') == 'Event':  # D1397, as :3495
            event = Event.query.filter_by(post_id=post.id).first()
            if event:
                # D1353, which is D1339 on the UPDATE side. Round 150 repaired the
                # same thirteen reads in `Post.new`'s Event branch and left this
                # copy alone: every one of them was `request_json['object'][...]`,
                # so an Event edit missing ANY key was a KeyError, and nine of
                # these are optional in the vocabulary. The two timestamps went
                # into `datetime.fromisoformat` directly, which is `ValueError:
                # Invalid isoformat string` for anything else -- and both raise out
                # of the inbox, so the edit was simply lost.
                #
                # Every field keeps its previous value when the peer does not send
                # a usable one, because an Update carries the whole object: a key
                # this instance cannot read is not the same as the peer clearing
                # the field, and guessing wrong in that direction silently erases
                # an event's details.
                event_json = request_json['object']
                start = parse_ap_timestamp(event_json.get('startTime'))
                if start is not None:
                    event.start = start
                end = parse_ap_timestamp(event_json.get('endTime'))
                if end is not None:
                    event.end = end
                if 'timezone' in event_json:
                    event.timezone = _as_text(event_json.get('timezone'), 30) or event.timezone
                if 'maximumAttendeeCapacity' in event_json:
                    event.max_attendees = _as_int(event_json.get('maximumAttendeeCapacity'),
                                                  event.max_attendees or 0)
                if 'participantCount' in event_json:
                    event.participant_count = _as_int(event_json.get('participantCount'),
                                                      event.participant_count or 0)
                if 'onlineLink' in event_json:
                    # D1403, as Post.new: a peer's url needs its scheme checked
                    # before a template makes it an href.
                    event.online_link = _as_url(event_json.get('onlineLink'), 1024)
                if 'joinMode' in event_json:
                    event.join_mode = _as_text(event_json.get('joinMode'), 10) or 'free'
                if 'externalParticipationUrl' in event_json:
                    event.external_participation_url = _as_url(
                        event_json.get('externalParticipationUrl'), 1024)
                if 'anonymousParticipation' in event_json:
                    event.anonymous_participation = bool(event_json.get('anonymousParticipation'))
                if 'isOnline' in event_json:
                    event.online = bool(event_json.get('isOnline'))
                if 'buyTicketsLink' in event_json:
                    event.buy_tickets_link = _as_url(event_json.get('buyTicketsLink'), 1024)
                if isinstance(event_json.get('attachment'), list):  # R223: absent from the list = removed
                    event.more_info_url = more_info_url_from(event_json.get('attachment'))
                if 'feeCurrency' in event_json:
                    event.event_fee_currency = _as_text(event_json.get('feeCurrency'), 4)
                if 'feeAmount' in event_json:
                    event.event_fee_amount = _as_float(event_json.get('feeAmount'),
                                                       event.event_fee_amount or 0)
                if post.image:
                    post.image.delete_from_disk()
                    old_db_entry_to_delete = post.image_id
                # D1352. `'url' in request_json['object']['image']` is a guard that
                # subscripts what it guards: on a bare-string `image` it is a
                # SUBSTRING test, so `image: "https://peer.test/url.png"` passed it
                # and the subscript below was then `TypeError: string indices must
                # be integers`; `image: 5` failed the guard itself with `argument
                # of type 'int' is not iterable`.
                image_url = image_url_from(request_json['object'].get('image'))
                if image_url:
                    image = File(source_url=image_url)
                    db.session.add(image)
                    db.session.commit()
                    post.image = image
                    if get_setting('cache_remote_images_locally', True):
                        make_image_sizes(image.id, 170, 512, 'posts')
                else:
                    post.image_id = None
                db.session.commit()

        # Links
        old_url = post.url
        # An Event with no Link attachment must compare EQUAL to what it already
        # holds, so the "this url has changed" arm below does not run: that arm's
        # `else` sets POST_TYPE_ARTICLE and clears image_id, silently turning an
        # event into a discussion and deleting its banner on an Update that
        # touched neither. This used to be the literal '' -- Post.new()'s Event
        # branch's own "no url" value -- which only matched events created that
        # way. app/shared/post.py:613 stores None for a locally created event
        # with a banner image, and Post.new() now does too (None is the column's
        # value for "no url", and post_to_page gates the outbound attachment on
        # `post.url is not None`, so '' federated `{"href": ""}` to peers).
        # Comparing old_url with itself matches whichever sentinel is stored,
        # including the '' in rows written before that change -- which is what
        # makes it safe without a migration. An attachment in this Update still
        # overwrites new_url below, so a real url change is still detected.
        new_url = old_url if post.type == POST_TYPE_EVENT else None
        # D1397. The `'type' in ...[0]` guard checked ONE element -- as a
        # membership test over whatever that element is, so a string element
        # containing 'type' passed it -- and then every element was subscripted
        # unguarded: `attachment['type']` is a KeyError for a dict without it and a
        # TypeError for a string. `_as_dict(...).get('type')` answers None for both,
        # which matches none of the arms. Every element is scanned, not only the
        # first, so a junk first entry cannot hide the attachments behind it (R202).
        if ('attachment' in request_json['object'] and
                isinstance(request_json['object']['attachment'], list) and
                len(request_json['object']['attachment']) > 0 and
                any('type' in _as_dict(attachment) for attachment in request_json['object']['attachment'])):

            for attachment in request_json['object']['attachment']:
                attachment = _as_dict(attachment)
                if attachment.get('type') == 'Link' and not is_more_info_link(attachment):  # R223: an event's own
                    if 'href' in attachment:
                        new_url = attachment['href']  # Lemmy < 0.19.4
                    elif 'url' in attachment:
                        new_url = attachment['url']  # NodeBB
                    if new_url:
                        break
                elif attachment.get('type') == 'Document':
                    new_url = attachment['url']  # Mastodon
                    if new_url:
                        break
                elif attachment.get('type') == 'Audio':  # WordPress podcast
                    new_url = attachment['url']
                    if 'name' in attachment:
                        post.title = attachment['name']
                    if new_url:
                        break
            # Lastly, check for image posts. Mbin sends link posts with both image and link and we want to ignore the image in that case.
            if not new_url:
                for attachment in request_json['object']['attachment']:
                    attachment = _as_dict(attachment)
                    if attachment.get('type') == 'Image':
                        new_url = attachment['url']  # PixelFed, PieFed, Lemmy >= 0.19.4

        if 'attachment' in request_json['object'] and isinstance(request_json['object']['attachment'],
                                                                 dict):  # Mastodon / a.gup.pe
            # D1397. A dict `attachment` without `url` was a KeyError here, and
            # the Update went with it; None falls through to the parse guard below.
            new_url = request_json['object']['attachment'].get('url')
        if new_url and not url_is_storable(new_url):
            # A peer-supplied attachment url urlparse refuses, or one naming a
            # scheme an href may not carry -- D1404, where an Update replaced an
            # already-stored https url with javascript:. Keep the url the post
            # already has (R213): clearing it would let a peer wipe a link by
            # naming a scheme we will not store, and rejecting the Update would
            # hand peers a way to make us drop their content. Also the only thing
            # standing between here and an AttributeError two lines down: since
            # the urlparse guard landed, domain_from_url returns None for these
            # rather than raising, and `new_domain.banned` would then be
            # 'NoneType' object has no attribute 'banned'.
            new_url = old_url
        new_domain = None
        if new_url:
            # `if new_domain and` for the same reason app/models.py's Post.new and
            # the three sites in app/shared/post.py use it: domain_from_url returns
            # None for a url whose host it cannot determine, and the parse guard
            # above does not cover the hostless-but-parseable case ('https:///x'
            # parses, .hostname is None). new_url is peer-supplied.
            new_domain = domain_from_url(new_url)
            if new_domain and new_domain.banned:
                db.session.commit()
                return  # reject change to url if new domain is banned
        if old_url != new_url:
            if post.image:
                post.image.delete_from_disk()
                old_db_entry_to_delete = post.image_id
            if new_url:
                thumbnail_url, embed_url = fixup_url(new_url)
                post.url = embed_url
                image = None
                if is_image_url(new_url):
                    post.type = POST_TYPE_IMAGE
                    image = File(source_url=new_url)
                    # _as_dict: element 0 may be a junk entry now that it does not gate the loop above (R202)
                    if isinstance(request_json['object']['attachment'], list) and \
                            _as_dict(request_json['object']['attachment'][0]).get('name') is not None:
                        image.alt_text = request_json['object']['attachment'][0]['name']
                else:
                    image_url = image_url_from(request_json['object'].get('image'))  # D1352
                    if image_url:
                        image = File(source_url=image_url)
                    else:
                        # Let's see if we can do better than the source instance did!
                        opengraph = opengraph_parse(thumbnail_url)
                        if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                            # D1405, as app/models.py: `og:image` reaches
                            # `File.source_url`, which is rendered as an href.
                            filename = _as_url(opengraph.get('og:image') or opengraph.get('og:image:url'), 1024)
                            if filename:
                                image = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                    if is_video_hosting_site(embed_url) or is_video_url(new_url):
                        post.type = POST_TYPE_VIDEO
                    else:
                        post.type = POST_TYPE_LINK
                if image:
                    db.session.add(image)
                    db.session.commit()
                    post.image = image
                    if get_setting('cache_remote_images_locally', True):
                        make_image_sizes(image.id, 170, 512, 'posts')  # the 512 sized image is for masonry view
                else:
                    old_db_entry_to_delete = None

                # url domain
                old_domain = domain_from_url(old_url) if old_url else None
                if new_domain and old_domain != new_domain:
                    # notify about links to banned websites.
                    already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
                    # D1391. `post.domain` here is the OLD Domain object -- the
                    # new one is assigned below -- and `Notification.targets` is a
                    # db.JSON column, so this could not be stored at all.
                    # Measured: `StatementError (builtins.TypeError) Object of
                    # type Domain is not JSON serializable`, which a peer reaches
                    # by editing a post's link to a domain whose `notify_mods` or
                    # `notify_admins` an admin has set.
                    #
                    # `suspect_user_user_name` is what
                    # app/templates/user/notifs/20.html:110 reads for this
                    # subtype; no producer wrote it.
                    targets_data = {'gen': '0',
                                    'post_id': post.id,
                                    'orig_post_title': post.title,
                                    'orig_post_body': post.body,
                                    'orig_post_domain': new_domain.name,
                                    'suspect_user_user_name': (
                                        post.author.ap_id if post.author and post.author.ap_id
                                        else post.author.user_name if post.author else ''),
                                    }
                    if new_domain.notify_mods:
                        for community_member in post.community.moderators():
                            # local moderators only, as edit_post does: a remote one never sees the row (D288)
                            if community_member.user.is_local():
                                notify = Notification(title='Suspicious content', url=post.ap_id,
                                                      user_id=community_member.user_id,
                                                      author_id=1, notif_type=NOTIF_REPORT,
                                                      subtype='post_from_suspicious_domain',
                                                      targets=targets_data)
                                db.session.add(notify)
                                already_notified.add(community_member.user_id)
                    if new_domain.notify_admins:
                        for admin in Site.admins():
                            if admin.id not in already_notified:
                                # D1391. Rebuilt per admin with `post.domain`, the
                                # same unserialisable object as above; the dict
                                # built once before the loop already holds
                                # everything this needs.
                                notify = Notification(title='Suspicious content',
                                                      url=post.ap_id, user_id=admin.id,
                                                      author_id=1, notif_type=NOTIF_REPORT,
                                                      subtype='post_from_suspicious_domain',
                                                      targets=targets_data)
                                db.session.add(notify)
                    new_domain.post_count += 1
                    post.domain = new_domain

                # Fix-up cross posts (Posts which link to the same url as other posts)
                if post.cross_posts is not None:
                    post.calculate_cross_posts(url_changed=True)

            else:
                post.type = POST_TYPE_ARTICLE
                # None, not '': Post.url is nullable with no default, so None is
                # what the column holds for a post that has no url, and it is what
                # the other peer-supplied write in this function (the microblog
                # branch, ~line 2902) already stores. Every consumer in app/ and in
                # the templates either reads post.url for truth ('if post.url:',
                # '{% if post.url %}'), which cannot tell '' from None, or is gated
                # on a post type this branch cannot produce -- it sets
                # POST_TYPE_ARTICLE on the line above. '' additionally made this
                # branch non-idempotent: new_url is initialised to None for every
                # non-Event type (~line 3111), so a stored '' made `old_url !=
                # new_url` true again on the NEXT Update and re-ran this whole arm
                # -- clearing image_id and recalculating cross posts -- every time.
                post.url = None
                post.image_id = None
                if post.cross_posts is not None:  # unlikely, but not impossible
                    post.calculate_cross_posts(delete_only=True)

        db.session.commit()
        if old_db_entry_to_delete:
            File.query.filter_by(id=old_db_entry_to_delete).delete()
            db.session.commit()

        # An Update that carries attachments replaces the album, as it replaces the image
        if 'attachment' in request_json['object']:
            set_post_gallery(post, request_json, post.community.low_quality)


def undo_vote(comment, post, target_ap_id, user):
    voted_on = find_liked_object(target_ap_id)
    if isinstance(voted_on, Post):
        post = voted_on
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=post.id).first()
        if existing_vote:
            with db.session.begin_nested():
                db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                   {'effect': existing_vote.effect, 'user_id': post.user_id})
            if existing_vote.effect < 0:  # Lemmy sends 'like' for upvote and 'dislike' for down votes. Cool! When it undoes an upvote it sends an 'Undo Like'. Fine. When it undoes a downvote it sends an 'Undo Like' - not 'Undo Dislike'?!
                post.down_votes -= 1
            else:
                post.up_votes -= 1
            post.score -= existing_vote.effect
            db.session.delete(existing_vote)
            db.session.commit()
        return post
    if isinstance(voted_on, PostReply):
        comment = voted_on
        existing_vote = PostReplyVote.query.filter_by(user_id=user.id, post_reply_id=comment.id).first()
        if existing_vote:
            with db.session.begin_nested():
                db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                                   {'effect': existing_vote.effect, 'user_id': comment.user_id})
            if existing_vote.effect < 0:  # Lemmy sends 'like' for upvote and 'dislike' for down votes. Cool! When it undoes an upvote it sends an 'Undo Like'. Fine. When it undoes a downvote it sends an 'Undo Like' - not 'Undo Dislike'?!
                comment.down_votes -= 1
            else:
                comment.up_votes -= 1
            comment.score -= existing_vote.effect
            db.session.delete(existing_vote)
            db.session.commit()
        return comment

    return None


def undo_boost(target_ap_id: str, user: User) -> Union[Post, None]:
    """Remove `user`'s boost of the post at `target_ap_id`.

    Returns the post so the caller can log a result, mirroring undo_vote().
    A post with no boost from this user still returns the post: a repeated Undo
    is a successful no-op, not a failure.

    This function does not log; the routes.py call site is the sole logger.
    This mirrors undo_vote() and is intentionally opposite to
    process_announce_of_uri(), which self-logs on all paths.
    """
    if not target_ap_id:
        return None
    post = Post.get_by_ap_id(target_ap_id)
    if not post:
        return None
    remove_boost(post, user)
    return post


def process_report(user, reported, request_json, session) -> bool:
    """Record a peer's Flag, and say whether it was recorded.

    D1401. The return value is new. `find_reported_object` resolves the flagged id
    through `find_actor_or_create`, whose own signature is
    `Union[User, Community, Feed, None]` -- so a peer flagging one of our COMMUNITIES
    reaches the `elif isinstance(reported, Community): ...` arm below, which is a bare
    ellipsis, and a peer flagging a Feed matches no arm at all. Either way this
    function did nothing and said nothing, and the caller went on to log
    `APLOG_REPORT, APLOG_SUCCESS` and fan the Flag out to the moderators' instances.

    Measured:

        find_reported_object(<a community actor url>)  ->  Community
        process_report(reporter, community, ...)       ->  0 Report rows
                                                           0 Notifications

    So a report about a community was accepted, announced to other instances, logged
    as a success, and recorded nowhere: no row in `/admin/reports`, no notification,
    nothing a local admin could ever see. The log said the opposite of what happened,
    which is worse than the missing feature -- an operator reading it has no reason to
    look.

    The Community and Conversation arms are still unimplemented. Implementing them is
    a feature, not a repair: a community report needs the `targets` dict D1393 defined
    for `community_report.html` (`suspect_community_name`, `reporter_user_name`) and a
    notification subtype, and a conversation report needs the same for
    `conversation_report.html`. What this change fixes is the claim, so the caller can
    log a report it dropped as dropped.
    """
    if 'summary' not in request_json:  # reports from peertube have no summary
        reasons = ''
        description = ''
        if 'content' in request_json:
            reasons = request_json['content']
    else:
        reasons = request_json['summary']
        description = ''

    if isinstance(reported, User):
        if reported.reports == -1:
            # `-1` means this target is exempt from reports. Nothing is recorded, so
            # the caller must not log a success either (D1401).
            return False
        type = REPORT_TYPE_USER
        source_instance = session.get(Instance, user.instance_id)
        targets_data = {'gen': '0',
                        'suspect_user_id': reported.id,
                        'suspect_user_user_name': reported.ap_id if reported.ap_id else reported.user_name,
                        'reporter_id': user.id,
                        'reporter_user_name': user.ap_id if user.ap_id else user.user_name,
                        'source_instance_id': user.instance_id,
                        'source_instance_domain': source_instance.domain if source_instance else '',
                        'reasons': reasons,
                        'description': description
                        }
        report = Report(reasons=reasons[:255], description=description[:255],
                        type=type, reporter_id=user.id, suspect_user_id=reported.id,
                        source_instance_id=user.instance_id, targets=targets_data)
        session.add(report)

        # Notify site admin
        already_notified = set()
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Reported user', url='/admin/reports', user_id=admin.id,
                                      author_id=user.id, notif_type=NOTIF_REPORT,
                                      subtype='user_reported',
                                      targets=targets_data)
                session.add(notify)
                admin.unread_notifications += 1
        reported.reports += 1
        session.commit()
        return True
    elif isinstance(reported, Post):
        if reported.reports == -1:
            # `-1` means this target is exempt from reports. Nothing is recorded, so
            # the caller must not log a success either (D1401).
            return False
        type = REPORT_TYPE_POST
        suspect_author = session.get(User, reported.author.id)
        source_instance = session.get(Instance, user.instance_id)
        targets_data = {'gen': '0',
                        'suspect_post_id': reported.id,
                        'suspect_user_id': reported.author.id,
                        'suspect_user_user_name': suspect_author.ap_id if suspect_author.ap_id else suspect_author.user_name,
                        'reporter_id': user.id,
                        'reporter_user_name': user.ap_id if user.ap_id else user.user_name,
                        'source_instance_id': user.instance_id,
                        'source_instance_domain': source_instance.domain if source_instance else '',
                        'orig_post_title': reported.title,
                        'orig_post_body': reported.body
                        }
        report = Report(reasons=reasons[:255], description=description[:255], type=type, reporter_id=user.id,
                        suspect_user_id=reported.author.id, suspect_post_id=reported.id,
                        suspect_community_id=reported.community.id, in_community_id=reported.community.id,
                        source_instance_id=user.instance_id, targets=targets_data)
        session.add(report)

        already_notified = set()
        for mod in reported.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A post has been reported'),
                                        url=f"{current_app.config['SERVER_URL']}/post/{reported.id}",
                                        author_id=user.id, notif_type=NOTIF_REPORT,
                                        subtype='post_reported',
                                        targets=targets_data)
            session.add(notification)
            already_notified.add(mod.user_id)

        if reported.community.is_local() and reported.community.un_moderated:
            # Notify site admin if community is un-moderated
            already_notified = set()
            for admin in Site.admins():
                if admin.id not in already_notified:
                    # D1392. This is the POST branch, and the moderators above
                    # are told `post_reported` for the same report -- the admin
                    # was told `user_reported`, so
                    # app/templates/user/notifs/20.html rendered them the block
                    # for a reported USER: the wrong heading, and no post title
                    # or body, though `targets_data` carries both. That block
                    # reads `targets.reasons` and `targets.description`, which a
                    # post report's dict does not have, so its detail panel
                    # could not render either.
                    notify = Notification(title=_('A post has been reported'), url='/admin/reports', user_id=admin.id,
                                          author_id=user.id, notif_type=NOTIF_REPORT,
                                          subtype='post_reported',
                                          targets=targets_data)
                    session.add(notify)
                    admin.unread_notifications += 1
                    session.commit()

        reported.reports += 1
        session.commit()
        return True
    elif isinstance(reported, PostReply):
        if reported.reports == -1:
            # `-1` means this target is exempt from reports. Nothing is recorded, so
            # the caller must not log a success either (D1401).
            return False
        type = REPORT_TYPE_REPLY
        post = session.get(Post, reported.post_id)
        suspect_author = session.get(User, reported.author.id)
        source_instance = session.get(Instance, user.instance_id)
        targets_data = {'gen': '0',
                        'suspect_comment_id': reported.id,
                        'suspect_user_id': reported.author.id,
                        'suspect_user_user_name': suspect_author.ap_id if suspect_author.ap_id else suspect_author.user_name,
                        'reporter_id': user.id,
                        # `user_name`, not `name`: `User` has no `name`, so a
                        # reporter with no ap_id -- which is what a LOCAL actor
                        # is -- was an AttributeError here and the report was
                        # never filed. The two branches above spell it
                        # correctly.
                        'reporter_user_name': user.ap_id if user.ap_id else user.user_name,
                        'source_instance_id': user.instance_id,
                        'source_instance_domain': source_instance.domain if source_instance else '',
                        'orig_comment_body': reported.body
                        }
        report = Report(reasons=reasons[:255], description=description[:255], type=type, reporter_id=user.id,
                        suspect_post_id=post.id,
                        suspect_community_id=post.community.id,
                        suspect_user_id=reported.author.id, suspect_post_reply_id=reported.id,
                        in_community_id=post.community.id,
                        source_instance_id=user.instance_id,
                        targets=targets_data)
        session.add(report)
        # Notify moderators
        already_notified = set()
        for mod in post.community.moderators():
            notification = Notification(user_id=mod.user_id, title=_('A comment has been reported'),
                                        url=f"{current_app.config['SERVER_URL']}/comment/{reported.id}",
                                        author_id=user.id, notif_type=NOTIF_REPORT,
                                        subtype='comment_reported',
                                        targets=targets_data)
            session.add(notification)
            already_notified.add(mod.user_id)

        if reported.community.is_local() and reported.community.un_moderated:
            # Notify site admin if community is un-moderated
            already_notified = set()
            for admin in Site.admins():
                if admin.id not in already_notified:
                    # D1392, the same mislabelling in the PostReply branch: the
                    # moderators above get `comment_reported`, the admin got
                    # `user_reported`.
                    notify = Notification(title=_('A comment has been reported'), url='/admin/reports', user_id=admin.id,
                                          author_id=user.id, notif_type=NOTIF_REPORT,
                                          subtype='comment_reported',
                                          targets=targets_data)
                    session.add(notify)
                    admin.unread_notifications += 1
                    session.commit()

        reported.reports += 1
        session.commit()
        return True
    elif isinstance(reported, Community):
        # D1401. Unimplemented, and now honest about it -- see this function's
        # docstring for why implementing it is a feature rather than a repair.
        return False
    elif isinstance(reported, Conversation):
        return False
    # Anything else `find_reported_object` can hand us, which today means a Feed:
    # `find_actor_or_create` returns one and no arm above matches it.
    return False


def process_quote_boost(core_activity: dict, post_ap: str, their_post_ap: str):
    post = Post.get_by_ap_id(post_ap)
    if post is None:
        post = PostReply.get_by_ap_id(post_ap)
    if post is not None and post.author.is_local():
        # R205: the decision is recorded, so /quote_boost_auth vouches for this quote and no other
        quoted = {'post_id': post.id} if isinstance(post, Post) else {'post_reply_id': post.id}
        if QuoteAuthorization.query.filter_by(quoting_uri=their_post_ap, **quoted).first() is None:
            db.session.add(QuoteAuthorization(quoting_uri=their_post_ap, **quoted))
            db.session.commit()

        accept_activity = {
          "@context": [
            "https://www.w3.org/ns/activitystreams",
            {
              "QuoteRequest": "https://w3id.org/fep/044f#QuoteRequest"
            }
          ],
          "type": "Accept",
          "to": core_activity['actor'],
          "id": f"{current_app.config['SERVER_URL']}/activities/{gibberish(15)}",
          "actor": post.author.public_url(),
          "object": core_activity,
          "result": f"{current_app.config['SERVER_URL']}/quote_boost_auth?stamp={urllib.parse.quote(post.public_url(), safe='')};{urllib.parse.quote(their_post_ap, safe='')}"
        }
        to = find_actor_or_create_cached(core_activity['actor'])
        if to and to.instance.inbox:
            send_post_request(to.instance.inbox, accept_activity, post.author.private_key, post.author.public_url() + '#main-key')


def announcer_is_followed(user_id: int) -> bool:
    """True if at least one local user follows the remote user with this id.

    Called before any outbound fetch, so that an unfollowed remote party cannot
    make this instance request a URL of their choosing.

    A follow the remote side has explicitly rejected (is_accepted is False) does
    not open the gate. A pending (is_accepted is None) or accepted (True) follow
    does: Mastodon follows can sit pending indefinitely and those users still
    expect their timeline to work. `isnot(False)` (rather than `!= False`) is
    required so NULL (pending) rows are not excluded by SQL's NULL comparison
    semantics.
    """
    return db.session.query(UserFollower.id).filter(
        UserFollower.remote_user_id == user_id,
        UserFollower.is_inward == False,
        UserFollower.is_accepted.isnot(False)).first() is not None


def record_boost(post: Post, user: User) -> None:
    """Record that `user` boosted `post`, idempotently, and refresh the cache.

    Idempotent by query-then-insert: the same Announce can be redelivered after
    the 90 second Redis duplicate window in routes.py has expired.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing:
        return
    db.session.add(PostBoost(user_id=user.id, post_id=post.id))
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()


def remove_boost(post: Post, user: User) -> None:
    """Remove `user`'s boost of `post` and refresh the cache.

    A missing row is a successful no-op, not a failure — remote instances re-send.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing is None:
        return
    db.session.delete(existing)
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()


AS_PUBLIC = ('https://www.w3.org/ns/activitystreams#Public', 'as:Public', 'Public')


def _addressing_list(obj: dict, field: str) -> list:
    """Normalise one ActivityPub addressing field to a list of strings.

    `to` and `cc` are each independently a string, a list, or absent.
    """
    if not isinstance(obj, dict):
        return []
    value = obj.get(field)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    return []


def activitypub_visibility(obj: dict) -> str:
    """Classify an object's audience as 'public', 'unlisted', 'followers' or 'direct'.

    Pass the OBJECT, never the wrapping activity. In the boost path
    create_resolved_object() synthesises {'id': ..., 'object': post_data} with no
    addressing at the activity level, so reading the activity would see nothing and
    classify everything as public.

    Note that Post.private does NOT mean "not public": Post.new() sets it for any
    titleless object, i.e. every microblog post, making it an unlisted marker.
    PostReply.private is no longer read or written: a reply's visibility column
    carries followers-only.

    Also note Post.new() clears that private flag whenever the ACTIVITY-level
    'to' or 'cc' contains Public (app/models.py ~1802-1807), including via 'cc' --
    i.e. for a genuinely unlisted post. This classifier reads the OBJECT's own
    'to'/'cc' instead. So a directly-delivered unlisted Mastodon post ends up
    stored with private=False (Post.new's activity-level check) even though this
    classifier would call it 'unlisted' (the object-level check): two sources of
    truth for addressing, five hundred lines apart. Do not conflate them.
    """
    to = _addressing_list(obj, 'to')
    cc = _addressing_list(obj, 'cc')

    if any(addr in AS_PUBLIC for addr in to):
        return 'public'
    if any(addr in AS_PUBLIC for addr in cc):
        return 'unlisted'
    if any(addr.endswith('/followers') for addr in to + cc):
        return 'followers'
    return 'direct'


def announce_target_uri(activity: dict) -> Union[str, None]:
    """Return the URI of the object an Announce (or Undo/Announce) refers to.

    Mastodon sends the object as a bare URI string. Some platforms embed the
    object, in which case its 'id' is the URI. Returns None if neither is usable.
    """
    if not isinstance(activity, dict):
        return None
    obj = activity.get('object')
    if isinstance(obj, str):
        return obj if obj else None
    if isinstance(obj, dict):
        obj_id = obj.get('id')
        return obj_id if isinstance(obj_id, str) and obj_id else None
    return None


def is_top_level(post_data: dict) -> bool:
    """True if a fetched object is a top-level post rather than a reply."""
    if not isinstance(post_data, dict):
        return False
    return not post_data.get('inReplyTo')


def process_microblog_announce(request_json, id, store_ap_json) -> Union[Post, None]:
    """Ingest a boost of a microblog post from an account a local user follows.

    Top-level posts only. Boosted replies are ignored: backfilling absent
    ancestors would mean unbounded recursion against untrusted hosts.
    """
    saved_json = request_json if store_ap_json else None

    uri = announce_target_uri(request_json)
    if not uri:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Announce has no object URI')
        return None

    # Trust gate. Must stay above every network call in this function.
    actor = request_json.get('actor') if isinstance(request_json, dict) else None
    if not actor or not isinstance(actor, (str, dict)):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Announce has no usable actor')
        return None
    announcer = find_actor_or_create_cached(actor, create_if_not_found=False)
    if not announcer or not isinstance(announcer, User):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce actor is not a known user')
        return None
    # Defence in depth, not the primary defence: find_actor_or_create_cached()
    # already rejects a banned actor, on a cache miss and on a cache hit alike.
    if announcer.banned:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, f'{announcer.ap_id} is banned')
        return None
    if not announcer_is_followed(announcer.id):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce from unfollowed actor')
        return None

    # Local posts carry a full ap_id from Post.generate_ap_id(), so this resolves
    # both already-ingested remote posts and posts authored on this instance.
    post = Post.get_by_ap_id(uri)
    if post:
        # Local ap_ids are guessable (https://<server>/post/<id>), so any remote
        # actor a single local user follows could otherwise Announce an arbitrary
        # local post URI into a post_boost row -- including one in a private
        # (invite-only) or local_only community the announcer was never a member
        # of. Refuse before recording rather than trusting the community's own
        # federation gate, which this path never goes through.
        community = post.community
        if community and (community.private or community.local_only):
            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json,
                            'Boosted post belongs to a private or local_only community')
            return None
        record_boost(post, announcer)
        return post

    post_data = remote_object_to_json(uri)
    if not post_data:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Could not fetch boosted object ' + uri)
        return None

    if not is_top_level(post_data):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object is a reply')
        return None

    # create_resolved_object performs the attributedTo / domain-match impersonation
    # check. Do not duplicate it here.
    resolved = create_resolved_object(uri, post_data, host_of(uri),
                                      find_microblogging_community(), id, store_ap_json)
    if not isinstance(resolved, Post):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object did not resolve to a post')
        return None

    record_boost(resolved, announcer)
    return resolved


def process_announce_of_uri(request_json, community, id, store_ap_json) -> Union[Post, None]:
    """Route an Announce whose object is a bare URI.

    With no community, this is a microblog boost. Its object may be local content,
    in which case process_microblog_announce records the boost without fetching or
    creating anything -- which is why the local-content short-circuit below applies
    only to the community path.

    This function logs its own outcome on every path -- the caller must not log
    anything of its own on top of it. On the microblog path that means deferring
    entirely to process_microblog_announce, which already logs a distinct reason
    on every exit path itself (except the deliberate, pre-existing silent
    short-circuit for a boost of a post already held locally -- that one records
    the boost and returns it without logging anything, and this function must not
    add a log row there either). On the community path, this function logs
    success/failure itself, using the same log type, result, and message strings
    routes.py used to log at its call site before this function existed.
    (Note: Undo handlers like undo_boost() follow the opposite contract --
    they do not log, delegating to the caller instead.)
    """
    if community is None:
        return process_microblog_announce(request_json, id, store_ap_json)

    uri = announce_target_uri(request_json)
    if not uri:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, request_json if store_ap_json else None,
                        'Announce has no object URI')
        return None

    if uri.startswith('https://' + current_app.config['SERVER_NAME']):
        log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, request_json if store_ap_json else None,
                        'Activity about local content which is already present')
        return None

    post = resolve_remote_post(uri, community, id, store_ap_json)
    if post:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_SUCCESS, request_json)
    else:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, request_json, 'Could not resolve post')
    return post


def lemmy_site_data():
    site = g.site
    logo = site.logo if site.logo else '/static/images/piefed_logo_icon_t_75.png'
    data = {
        "site_view": {
            "site": {
                "id": 1,
                "name": site.name,
                "sidebar": site.sidebar,
                "published": site.created_at.isoformat(),
                "updated": site.updated.isoformat(),
                "icon": f"{current_app.config['SERVER_URL']}{logo}",
                "banner": "",
                "description": site.description,
                "actor_id": f"{current_app.config['SERVER_URL']}/",
                "last_refreshed_at": site.updated.isoformat(),
                "inbox_url": f"{current_app.config['SERVER_URL']}/inbox",
                "public_key": site.public_key,
                "instance_id": 1
            },
            "local_site": {
                "id": 1,
                "site_id": 1,
                "site_setup": True,
                "enable_downvotes": site.enable_downvotes,
                "enable_nsfw": site.enable_nsfw,
                "enable_nsfl": site.enable_nsfl,
                "community_creation_admin_only": site.community_creation_admin_only,
                "require_email_verification": True,
                "application_question": site.application_question,
                "private_instance": False,
                "default_theme": "browser",
                "default_post_listing_type": "All",
                "hide_modlog_mod_names": True,
                "application_email_admins": True,
                "actor_name_max_length": 20,
                "federation_enabled": True,
                "captcha_enabled": get_setting('captcha_enabled', True),
                "captcha_difficulty": "medium",
                "published": site.created_at.isoformat(),
                "updated": site.updated.isoformat(),
                "registration_mode": site.registration_mode,
                "reports_email_admins": site.reports_email_admins
            },
            "local_site_rate_limit": {
                "id": 1,
                "local_site_id": 1,
                "message": 999,
                "message_per_second": 60,
                "post": 50,
                "post_per_second": 600,
                "register": 20,
                "register_per_second": 3600,
                "image": 100,
                "image_per_second": 3600,
                "comment": 100,
                "comment_per_second": 600,
                "search": 999,
                "search_per_second": 600,
                "published": site.created_at.isoformat(),
            },
            "counts": {
                "id": 1,
                "site_id": 1,
                "users": users_total(),
                "posts": local_posts(),
                "comments": local_comments(),
                "communities": local_communities(),
                "users_active_day": active_day(),
                "users_active_week": active_week(),
                "users_active_month": active_month(),
                "users_active_half_year": active_half_year()
            }
        },
        "admins": [],
        "version": current_app.config['VERSION'],
        "all_languages": [],
        "discussion_languages": [],
        "taglines": [],
        "custom_emojis": []
    }

    # Languages
    discussion_languages = []
    for language in Language.query.all():
        # hardcode English as the site language, for now. This will need to be an admin setting, soon.
        if language.code == 'und' or language.code == 'en':
            discussion_languages.append(language.id)
        data['all_languages'].append({
            'id': language.id,
            'code': language.code,
            'name': language.name
        })
    data['discussion_languages'] = discussion_languages

    # Custom emojis (only local instance emojis)
    local_emojis = Emoji.query.filter_by(instance_id=1).all()
    for emoji in local_emojis:
        # Extract shortcode from token (remove colons if present)
        shortcode = emoji.token.strip(':') if emoji.token else ''

        # Parse aliases (space-separated string) into keyword objects
        keywords = []
        if emoji.aliases:
            for alias in emoji.aliases.split():
                keywords.append({"keyword": alias})

        emoji_data = {
            "custom_emoji": {
                "shortcode": shortcode,
                "image_url": emoji.url,
                "category": emoji.category if emoji.category else ""
            },
            "keywords": keywords
        }
        data['custom_emojis'].append(emoji_data)

    # Admins (plus staff)
    for admin in Site.admins() + Site.staff():
        person = {
            "id": admin.id,
            "name": admin.user_name,
            "display_name": admin.display_name(),
            "avatar": 'https://' + current_app.config['SERVER_NAME'] + admin.avatar_image(),
            "banned": admin.banned,
            "published": admin.created.isoformat() + 'Z',
            "updated": admin.created.isoformat() + 'Z',
            "actor_id": admin.public_url(),
            "local": True,
            "deleted": admin.deleted,
            "matrix_user_id": admin.matrix_user_id,
            "admin": True,
            "bot_account": admin.bot,
            "instance_id": 1
        }
        counts = {
            "id": admin.id,
            "person_id": admin.id,
            "post_count": 0,
            "post_score": 0,
            "comment_count": 0,
            "comment_score": 0
        }
        data['admins'].append({'person': person, 'counts': counts, 'is_admin': True})
    return data


def ensure_domains_match(activity: dict) -> bool:
    if 'id' in activity:
        note_id = activity['id']
    else:
        note_id = None

    note_actor = None
    if 'actor' in activity:
        note_actor = activity['actor']
    elif 'attributedTo' in activity:
        attributed_to = activity['attributedTo']
        if isinstance(attributed_to, str):
            note_actor = attributed_to
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') in ('Person', 'Podcast'):
                    note_actor = a.get('id')
                    break
                elif isinstance(a, str):
                    note_actor = a
                    break

    if note_id and note_actor:
        id_domain = host_of(note_id)
        actor_domain = host_of(note_actor)

        if id_domain and id_domain == actor_domain:
            return True

    return False


def remote_object_to_json(uri):
    try:
        object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    except httpx.HTTPError:
        time.sleep(3)
        try:
            object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            return None
    if object_request.status_code == 200:
        try:
            object = object_request.json()
            return object
        except:
            object_request.close()
            return None
        finally:
            object_request.close()
    elif object_request.status_code == 401:
        site = db.session.get(Site, 1)
        try:
            object_request = signed_get_request(uri, site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
        except httpx.HTTPError:
            time.sleep(3)
            try:
                object_request = signed_get_request(uri, site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
            except httpx.HTTPError:
                return None
        try:
            object = object_request.json()
            return object
        except:
            return None
        finally:
            object_request.close()
    else:
        return None


# called from incoming activitypub, when the object in an Announce is just a URL
# despite the name, it works for both posts and replies
def resolve_remote_post(uri: str, community, announce_id, store_ap_json, nodebb=False) -> Union[Post, PostReply, None]:
    # Hosts, not authorities (D21): a community host with a capital or an
    # explicit port is the same host. host_of turns a URI urlparse rejects into
    # '', which is refused here rather than compared -- '' == '' is True.
    uri_domain = host_of(uri)
    if not uri_domain:
        return None
    announce_actor_domain = host_of(community.ap_profile_id)
    if announce_actor_domain != 'ovo.st' and not nodebb and announce_actor_domain != uri_domain:
        return None

    post_data = remote_object_to_json(uri)
    if not post_data:
        return None

    return create_resolved_object(uri, post_data, uri_domain, community, announce_id, store_ap_json)


def create_resolved_object(uri, post_data, uri_domain, community, announce_id, store_ap_json):
    # find the author. Make sure their domain matches the site hosting it to mitigate impersonation attempts
    # Hosts, not authorities (D22). uri_domain is an authority from the caller -- the alpha API's may carry a
    # port -- so it is normalised too, and '//' makes urlparse read it as one. An empty host is refused below
    # rather than compared, because host_of degrades what urlparse rejects to '' and '' == '' is True.
    uri_domain = host_of(f'//{uri_domain}') if uri_domain else ''
    actor_domain = None
    actor = None
    if 'attributedTo' in post_data:
        attributed_to = post_data['attributedTo']
        if isinstance(attributed_to, dict):  # a single embedded actor is walked like a one-element list (D38)
            attributed_to = [attributed_to]
        if isinstance(attributed_to, str):
            actor = attributed_to
            actor_domain = host_of(actor)
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') in ('Person', 'Podcast'):
                    actor = a.get('id')
                    if isinstance(actor, str):  # Ensure `actor` is a valid string
                        actor_domain = host_of(actor)
                    break
                elif isinstance(a, str):
                    actor = a
                    actor_domain = host_of(actor)
                    break
    if not uri_domain or uri_domain != actor_domain:
        return None

    user = find_actor_or_create(actor)
    if user and community and post_data:
        activity = 'update' if 'updated' in post_data else 'create'
        request_json = {'id': f"https://{uri_domain}/activities/{activity}/{gibberish(15)}", 'object': post_data}
        if 'inReplyTo' in request_json['object'] and request_json['object']['inReplyTo']:
            # PERM-3: the gate an inbound Create or Update of a reply passes (process_new_content)
            if not can_create_post_reply(user, community):
                return None
            if activity == 'update':
                post_reply = PostReply.get_by_ap_id(uri)
                if post_reply:
                    if post_reply.user_id != user.id:  # PERM-3: only the owner edits, as an inbox Update
                        return None
                    update_post_reply_from_activity(post_reply, request_json)
                else:
                    activity = 'create'
            if activity == 'create':
                post_reply = create_post_reply(store_ap_json, community, request_json['object']['inReplyTo'], request_json, user)
                if post_reply:
                    # D1340's third and fourth sites. Same shape as the tail of
                    # `resolve_remote_post`: a peer's string into a DateTime
                    # column, which is a DatatypeMismatch or a DataError at
                    # commit -- `UPDATE post SET posted_at=5` measured -- and the
                    # comment that had just been created goes with it.
                    published = parse_ap_timestamp(post_data.get('published'))
                    if published is not None:
                        post_reply.posted_at = published
                        post_reply.post.last_active = published
                        post_reply.community.last_active = utcnow()
                        db.session.commit()
            if post_reply:
                return post_reply
        else:
            if not can_create_post(user, community):  # PERM-3, as for a reply above
                return None
            if activity == 'update':
                post = Post.get_by_ap_id(uri)
                if post:
                    if post.user_id != user.id:  # PERM-3, as for a reply above
                        return None
                    update_post_from_activity(post, request_json)
                else:
                    activity = 'create'
            if activity == 'create':
                post = create_post(store_ap_json, community, request_json, user, announce_id)
                if post:
                    published = parse_ap_timestamp(post_data.get('published'))
                    if published is not None:
                        post.posted_at = published
                        post.last_active = published
                        post.community.last_active = utcnow()
                        db.session.commit()
            if post:
                return post

    return None


@celery.task
def get_nodebb_replies_in_background(replies_uri_list, community_id):
    """Fetch the first few replies of a NodeBB topic.

    D1340. `replies_uri_list` comes from a peer's `orderedItems`, sliced, and was
    iterated without being looked at. Measured:

      * a string -- `orderedItems: "https://..."` -- iterates its CHARACTERS, so
        this asked the peer for ten single-letter "uris";
      * None or a number is `TypeError: 'NoneType' object is not iterable`, which
        the `except` below re-raises;
      * a dict iterates its keys.

    And one reply that failed abandoned the rest, because the raise left the loop:
    nine good replies were dropped for one bad one. A reply that cannot be
    resolved is skipped now, and the exception is logged rather than lost.
    """
    try:
        max = 10 if not current_app.debug else 2  # magic number alert
        community = db.session.get(Community, community_id)
        if not community:
            return
        if not isinstance(replies_uri_list, list):
            return
        reply_count = 0
        for uri in replies_uri_list:
            if not isinstance(uri, str) or not uri:
                continue
            reply_count += 1
            try:
                resolve_remote_post(uri, community, None, False, nodebb=True)
            except Exception as e:
                current_app.logger.info(f'Could not resolve nodebb reply {uri}: {e}')
            if reply_count >= max:
                break
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


def populate_child_feed(feed_id, child_feed):
    if current_app.debug:
        populate_child_feed_worker(feed_id, child_feed)
    else:
        populate_child_feed_worker.delay(feed_id, child_feed)


@celery.task
def populate_child_feed_worker(feed_id, child_feed):
    try:
        from app.feed.util import search_for_feed  # cycle: importing app.feed runs app.feed.routes, which imports from this module
        server, feed = extract_domain_and_actor(child_feed)
        new_feed = search_for_feed('~' + feed + '@' + server, retry=True)
        new_feed.parent_feed_id = feed_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.remove()


# called from UI, via 'search' option in navbar, or 'Retrieve a post from the original server' in community sidebar,
# and from the Move activity handler with a peer-supplied URI
def resolve_remote_post_from_search(uri: str) -> Union[Post, None]:
    post = Post.get_by_ap_id(uri)
    if post:
        return post

    # Hosts, not authorities (D24), as in create_resolved_object: an empty host is refused at the gate below
    # rather than compared, because host_of degrades what urlparse rejects to '' and '' == '' is True.
    uri_domain = host_of(uri)
    actor_domain = None
    actor = None

    post_data = remote_object_to_json(uri)
    if not post_data:
        return None

    # nodebb. the post is the first entry in orderedItems of a topic, and the replies are the remaining entries
    # just gets orderedItems[0] to retrieve the post, and then replies are retrieved in the background
    topic_post_data = post_data
    nodebb = False
    ordered_items = None
    total_items = None
    if ('type' in post_data and post_data['type'] == 'Conversation' and
            'posts' in post_data and isinstance(post_data['posts'], str)):
        post_data = remote_object_to_json(post_data['posts'])
        if not post_data:
            return None
        topic_post_data = post_data
    # `isinstance(..., int)` rather than `'totalItems' in post_data`: the membership
    # test let a STRING through to `> 0`, which is `TypeError: '>' not supported
    # between instances of 'str' and 'int'` -- the same reason the sibling read
    # further down needed guarding (D1340).
    if ('type' in post_data and post_data['type'] == 'OrderedCollection' and
            isinstance(post_data.get('totalItems'), int) and post_data['totalItems'] > 0 and
            isinstance(post_data.get('orderedItems'), list) and post_data['orderedItems']):
        nodebb = True
        # Read both here, where the gate above has just established that one is a
        # list and the other an int. The tail of this function needed its own copy
        # of those two isinstance() tests as long as it re-read the keys itself,
        # and a guard that can never fail is a guard nothing can test (D1340).
        ordered_items = post_data['orderedItems']
        total_items = post_data['totalItems']
        uri = ordered_items[0]
        uri_domain = host_of(uri)
        post_data = remote_object_to_json(uri)
        if not post_data:
            return None

    # check again that it doesn't already exist (can happen with different but equivalent URLs)
    if not post_data.get('id'):  # a document with no id is refused, not a KeyError (D39)
        return None
    post = Post.get_by_ap_id(post_data['id'])
    if post:
        return post

    # find the author of the post. Make sure their domain matches the site hosting it to mitigate impersonation attempts
    if 'attributedTo' in post_data:
        attributed_to = post_data['attributedTo']
        if isinstance(attributed_to, dict):  # a single embedded actor is walked like a one-element list (D38)
            attributed_to = [attributed_to]
        if isinstance(attributed_to, str):
            actor = attributed_to
            actor_domain = host_of(actor)
        elif isinstance(attributed_to, list):
            for a in attributed_to:
                if isinstance(a, dict) and a.get('type') in ('Person', 'Podcast'):
                    actor = a.get('id')
                    if isinstance(actor, str):  # Ensure `actor` is a valid string
                        actor_domain = host_of(actor)
                    break
                elif isinstance(a, str):
                    actor = a
                    actor_domain = host_of(actor)
                    break
    if not uri_domain or uri_domain != actor_domain:
        return None

    # find the community the post was submitted to
    community = find_community(post_data)
    if not community and nodebb:
        community = find_community(topic_post_data)  # use 'audience' from topic if post has no info for how it got there
    # find the post's author
    user = find_actor_or_create(actor)
    if user and community and post_data:
        request_json = {'id': f"https://{uri_domain}/activities/create/{gibberish(15)}", 'object': post_data}
        # not really what this function is intended for, but get comment or fail if comment URL is searched for
        if 'inReplyTo' in post_data and post_data['inReplyTo'] is not None:
            in_reply_to = post_data['inReplyTo']
            # PERM-4: the gate an inbound Create passes (process_new_content), as in create_resolved_object
            if not can_create_post_reply(user, community):
                return None
            object = create_post_reply(False, community, in_reply_to, request_json, user)
        else:
            in_reply_to = None
            if not can_create_post(user, community):  # PERM-4
                return None
            object = create_post(False, community, request_json, user)
        if object:
            if 'published' in post_data:
                # D1340. The peer's string went straight into `posted_at`, a
                # DateTime column -- and `last_active`, which round 141 made NOT
                # NULL -- so `published: "whenever"` was a DataError at commit that
                # poisoned the transaction and lost the post it had just created.
                published = parse_ap_timestamp(post_data['published'])
                if published is not None:
                    object.posted_at = published
                    if not in_reply_to:
                        object.last_active = published
                    else:
                        # D1342. This used to set nothing at all for a reply, so
                        # the parent kept the local now that `PostReply.new` gave
                        # it -- and `last_active` is what orders a community's
                        # listings, so resolving a two-year-old reply from search
                        # promoted its whole thread to the top of the community.
                        # `create_resolved_object`, which handles the very same
                        # document when it arrives in an inbox instead, has always
                        # used the reply's `published` here.
                        object.post.last_active = published
                    db.session.commit()
            # These two keys used to be re-read here, unguarded, seventy lines
            # after the careful read above: a topic without `totalItems` was a
            # KeyError, and a `totalItems` that is a string was `TypeError: '>'
            # not supported between instances of 'str' and 'int'`. They are now
            # carried down from the gate that vetted them (D1340).
            if nodebb and total_items > 1:
                if current_app.debug:
                    get_nodebb_replies_in_background(ordered_items[1:], community.id)
                else:
                    get_nodebb_replies_in_background.delay(ordered_items[1:], community.id)
            return object if not in_reply_to else object.post

    return None


# called from activitypub/routes if something is posted to us without any kind of signature (typically from PeerTube)
def verify_object_from_source(request_json) -> Tuple[Union[dict, None], Union[str, None]]:
    """Decide whether the object URI an unsigned activity names really belongs
    to the peer that announced it, and on success replace request_json's
    'object' with the fetched document.

    Returns `(request_json, None)` on success and `(None, reason)` on every
    refusal, where the reason names which check refused. The caller writes it
    into the incoming-activity log: without it every one of these paths logged
    the same sentence, and an operator holding that log could not tell a
    malformed URI from an impersonation attempt from a peer that was simply
    unreachable.

    HOSTS, NOT AUTHORITIES. Both comparisons go through host_of, so a peer
    that is inconsistent about the port between 'actor', 'object' and the
    fetched 'attributedTo' is no longer falsely refused -- the port and any
    userinfo are not part of who the peer is. host_of degrades an unparseable
    URL to '', and '' == '' is true, so an empty host must never be allowed to
    satisfy either comparison. Nothing here compares two possibly-empty hosts:
    the `not uri_domain` guard below returns before either comparison runs, so
    uri_domain is non-empty at both of them and an empty create_domain or
    actor_domain can only ever compare unequal. Any later edit that weakens or
    reorders that first guard has to restore the emptiness check explicitly.
    """
    uri = request_json['object']
    uri_domain = host_of(uri)
    if not uri_domain:
        return None, 'the object URI has no host'

    create_domain = host_of(request_json['actor'])
    if create_domain != uri_domain:
        return None, 'the announcing actor is on a different host than the object URI'

    try:
        object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
    except httpx.HTTPError:
        time.sleep(3)
        try:
            object_request = get_request(uri, headers={'Accept': 'application/activity+json'})
        except httpx.HTTPError:
            return None, 'the object could not be fetched'
    if object_request.status_code == 200:
        try:
            object = object_request.json()
        except JSONDecodeError:
            object_request.close()
            return None, 'the object response was not JSON'
        object_request.close()
    elif object_request.status_code == 401:
        site = db.session.get(Site, 1)
        if site is None:
            return None, 'the object needs a signed fetch and there is no Site row to sign it'
        try:
            object_request = signed_get_request(uri, site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
        except httpx.HTTPError:
            time.sleep(3)
            try:
                object_request = signed_get_request(uri, site.private_key, f"{current_app.config['SERVER_URL']}/actor#main-key")
            except httpx.HTTPError:
                return None, 'the object could not be fetched with a signed request'
        try:
            object = object_request.json()
        except JSONDecodeError:
            object_request.close()
            return None, 'the signed object response was not JSON'
        object_request.close()
    else:
        return None, f'the object fetch returned HTTP {object_request.status_code}'

    if not 'id' in object or not 'type' in object or not 'attributedTo' in object:
        return None, 'the fetched object has no id, type or attributedTo'

    actor_domain = ''
    if isinstance(object['attributedTo'], str):
        actor_domain = host_of(object['attributedTo'])
    elif isinstance(object['attributedTo'], dict) and 'id' in object['attributedTo']:
        actor_domain = host_of(object['attributedTo']['id'])
    elif isinstance(object['attributedTo'], list):
        for a in object['attributedTo']:
            if isinstance(a, str):
                actor_domain = host_of(a)
                break
            elif isinstance(a, dict) and a.get('type') in ('Person', 'Podcast'):
                actor = a.get('id')
                if isinstance(actor, str):
                    actor_domain = host_of(actor)
                break
    else:
        return None, 'the fetched object has an attributedTo of an unusable type'

    if uri_domain != actor_domain:
        return None, 'the fetched object is attributed to a different host than its URI'

    request_json['object'] = object
    return request_json, None


def log_incoming_ap(id, aplog_type, aplog_result, saved_json, message=None, session=None):
    aplog_in = APLOG_IN

    if aplog_in and aplog_type[0] and aplog_result[0]:
        if current_app.config['LOG_ACTIVITYPUB_TO_DB']:
            activity_log = ActivityPubLog(direction='in', activity_id=id, activity_type=aplog_type[1], result=aplog_result[1])
            if message:
                activity_log.exception_message = message
            if saved_json:
                activity_log.activity_json = json.dumps(saved_json)
            if session:
                session.add(activity_log)
                session.commit()
            else:
                db.session.add(activity_log)
                db.session.commit()

        if current_app.config['LOG_ACTIVITYPUB_TO_FILE']:
            current_app.logger.info(f'piefed.social activity: {id} Type: {aplog_type[1]}, Result: {aplog_result[1]}, {message}')


def find_community(request_json):
    # Create/Update from platform that included Community in 'audience', 'cc', or 'to' in outer or inner object
    # Also works for manually retrieved posts
    locations = ['audience', 'cc', 'to', 'target']
    if 'object' in request_json and isinstance(request_json['object'], dict):
        rjs = [request_json, request_json['object']]
    else:
        rjs = [request_json]
    for rj in rjs:
        for location in locations:
            if location in rj:
                potential_id = rj[location]
                if isinstance(potential_id, str):
                    if not potential_id.startswith('https://www.w3.org') and not potential_id.endswith('/followers'):
                        potential_community = db.session.query(Community).filter_by(ap_profile_id=potential_id.lower()).first()
                        if potential_community:
                            return potential_community
                if isinstance(potential_id, list):
                    for c in potential_id:
                        if isinstance(c, str) and not c.startswith('https://www.w3.org') and not c.endswith('/followers'):
                            potential_community = db.session.query(Community).filter_by(ap_profile_id=c.lower()).first()
                            if potential_community:
                                return potential_community

    # D1397. `request_json['object'] if 'object' in request_json` -- unguarded,
    # although the `rjs` list eleven lines above takes the same value only
    # `isinstance(..., dict)`. So a peer whose `object` is a string put a string in
    # `rj`, and `rj.get('type')` below is
    # `AttributeError: 'str' object has no attribute 'get'`. Measured:
    #
    #     find_community({'type': 'Create', 'object': 'https://peer.test/p/1'})
    #         AttributeError: 'str' object has no attribute 'get'
    #     ... 'object': 'https://peer.test/inReplyTo/1'
    #         TypeError: string indices must be integers   (substring test above)
    #     ... 'object': 42  /  None
    #         TypeError: argument of type 'int' is not iterable
    #     ... 'object': ['a']
    #         AttributeError: 'list' object has no attribute 'get'
    #
    # A string `object` is not an edge case: Lemmy's `Add` and `Remove` name their
    # object by url, and `app/activitypub/routes.py:1428` and `:1501` call this
    # with the whole activity whenever one arrives unannounced.
    #
    # Falling back to the outer activity is what the `else` arm already did for an
    # activity with no `object` at all, and the two checks below -- `inReplyTo` and
    # `type == 'Video'` -- are as meaningful there.
    rj = request_json['object'] if isinstance(request_json.get('object'), dict) else request_json

    # Create/Update Note from platform that didn't include the Community in 'audience', 'cc', or 'to' (e.g. Mastodon reply to Lemmy post)
    if 'inReplyTo' in rj and rj['inReplyTo'] is not None:
        post_being_replied_to = Post.get_by_ap_id(rj['inReplyTo'])
        if post_being_replied_to:
            return post_being_replied_to.community
        else:
            comment_being_replied_to = PostReply.get_by_ap_id(rj['inReplyTo'])
            if comment_being_replied_to:
                return comment_being_replied_to.community

    # Update / Video from PeerTube (possibly an edit, more likely an invite to query Likes / Replies endpoints)
    if rj.get('type') == 'Video':
        if 'attributedTo' in rj and isinstance(rj['attributedTo'], list):
            for a in rj['attributedTo']:
                if isinstance(a, str):
                    potential_community = Community.query.filter_by(ap_profile_id=a.lower()).first()
                    if potential_community:
                        return potential_community
                # D1397. `a['type']` after the `isinstance(a, str)` arm above, so
                # `a` is not a string -- but a dict without `type`, or a number, or
                # a nested list, was a KeyError or a TypeError out of the inbox.
                elif _as_dict(a).get('type') == 'Group' and isinstance(_as_dict(a).get('id'), str):
                    potential_community = db.session.query(Community).filter_by(ap_profile_id=a['id'].lower()).first()
                    if potential_community:
                        return potential_community

    return None


def find_microblogging_community():
    """ Make a special community to put posts without a community into, e.g. microblog posts """
    community = db.session.query(Community).filter(Community.instance_id == 1, Community.user_id == 1, Community.name == 'microblogs').first()
    if community is None:
        private_key, public_key = RsaKeys.generate_keypair()
        community = Community(title=_('Microblogs'), name='microblogs',
                              description=_('Microblog posts from around the fediverse.'),
                              nsfw=False, private_key=private_key,
                              public_key=public_key, description_html=markdown_to_html(_('Microblog posts from around the fediverse.')),
                              local_only=False, show_popular=False, show_all=False,
                              ap_profile_id='https://' + current_app.config['SERVER_NAME'] + '/c/microblogs',
                              ap_public_url='https://' + current_app.config['SERVER_NAME'] + '/c/microblogs',
                              ap_followers_url='https://' + current_app.config['SERVER_NAME'] + '/c/microblogs/followers',
                              ap_moderators_url='https://' + current_app.config['SERVER_NAME'] + '/c/microblogs/moderators',
                              ap_domain=current_app.config['SERVER_NAME'],
                              subscriptions_count=0, instance_id=1, user_id=1, ai_generated=False,
                              first_federated_at=utcnow())
        db.session.add(community)
        db.session.commit()
    return community


def normalise_actor_string(actor: str) -> Tuple[str, str]:
    # Turns something like whatever@server.tld into tuple(whatever, server.tld).
    # Anything that is not exactly that shape is ('', ''), which every caller
    # already tests for: `actor[0]` on an empty string was IndexError, and
    # `parts[1]` on `a@b@c` answered ('a', 'b') -- a lookup against a server
    # nobody named.
    if not actor:
        return '', ''
    actor = actor.strip()
    if actor and (actor[0] == '@' or actor[0] == '!' or actor[0] == '~'):
        actor = actor[1:]

    parts = actor.split('@')
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return '', ''
    return parts[0].lower(), parts[1].lower()


def process_banned_message(banned_json, instance_domain: str, session):
    """Record that a peer has told us one of our users is banned from it.

    Reached from app/activitypub/signature.py when a delivery of ours comes back
    `400` with `person_is_banned_from_site` in the body. Both the status and the
    body are the peer's choice, so everything here is untrusted input.

    D1379. `banned_json['message']` was read by hand: absent was `KeyError` and a
    non-string was `AttributeError: ... has no attribute 'strip'` from inside
    `find_actor_or_create`. The caller catches both -- into a logged failure --
    but its `result.close()` sits after this branch, so a raise here leaked the
    HTTP response, on every delivery to a peer that chose to answer this way.
    D1372's family at a fifth site.
    """
    actor = _as_text(banned_json.get('message') if isinstance(banned_json, dict)
                     else None)
    if actor is None:
        return
    if banned_person := find_actor_or_create(actor, create_if_not_found=False):
        instance = session.query(Instance).filter(Instance.domain == instance_domain.lower()).first()
        if instance:
            session.execute(text(
                '''
                INSERT INTO "instance_ban" (user_id, instance_id, banned_until)
                VALUES (:user_id, :instance_id, :banned_until)
                ON CONFLICT (user_id, instance_id)
                DO UPDATE SET banned_until = EXCLUDED.banned_until
                '''
            ), {
                "user_id": banned_person.id,
                "instance_id": instance.id,
                "banned_until": utcnow() + timedelta(days=1)
            })
            session.commit()


def is_vote(activity: dict) -> bool:
    try:
        if 'type' in activity:
            if activity['type'] == 'Announce':
                if 'object' in activity and isinstance(activity['object'], dict) and (activity['object']['type'] == 'Like' or activity['object']['type'] == 'Dislike'):
                    return True
            elif activity['type'] == 'Like' or activity['type'] == 'Dislike':
                return True
    except Exception:
        return False

    return False


def proactively_delete_content(community: Community, ap_id: str):
    return  # disable for now, seems buggy
    deletor = None
    # Try to find a local moderator to send the Delete
    for moderator in community.moderators():
        moderator_account = db.session.get(User, moderator.user_id)
        if moderator_account.is_local():
            deletor = moderator_account
            break
    # Use admin account if there is not one.
    if deletor is None:
        deletor = db.session.get(User, 1)
    if deletor:

        delete_id = f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}"
        to = ["https://www.w3.org/ns/activitystreams#Public"]
        cc = [community.public_url()]
        delete = {
          'id': delete_id,
          'type': 'Delete',
          'actor': deletor.public_url(),
          'object': ap_id,
          'audience': community.public_url(),
          'to': to,
          'cc': cc,
          'summary': 'Automatic deletion due to block'
        }

        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        actor = community.public_url()
        cc = [community.ap_followers_url]
        to = ["https://www.w3.org/ns/activitystreams#Public"]
        announce = {
            'id': announce_id,
            'type': 'Announce',
            'actor': actor,
            'object': delete,
            '@context': default_context(),
            'to': to,
            'cc': cc
        }
        domain = furl(ap_id).host
        instance = find_instance_by_domain(domain)
        if instance and instance.inbox:
            send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')


def object_has_missing_fields(object):
    # Validate the 'object' part of an Activity
    if 'type' in object and object['type'] == 'OrderedCollection':
        return not 'id' in object  # a collection has no actor/object, but the inbox still reads its id (D42)
    return not 'id' in object or not 'type' in object or not 'actor' in object or not 'object' in object
