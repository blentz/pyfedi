from __future__ import annotations

from datetime import datetime, timedelta
from time import sleep
from random import randint
from typing import List

import httpx
from PIL import Image, ImageOps
from flask import request, abort, g, current_app, json
from flask_login import current_user
from pillow_heif import register_heif_opener
# D1432. This was `from psycopg2 import IntegrityError`, which is NOT the class
# SQLAlchemy raises: a duplicate key arrives as `sqlalchemy.exc.IntegrityError`,
# whose MRO is DatabaseError -> DBAPIError -> StatementError -> SQLAlchemyError,
# with no psycopg2 ancestor at all. So neither `except IntegrityError` below ever
# fired, and a second backfill of the same community -- a search and an Announce
# arriving together -- aborted at the outer `except Exception: rollback; raise`,
# which is the community-half-filled failure those handlers were written to
# prevent.
from sqlalchemy.exc import IntegrityError
from flask_babel import _, lazy_gettext as _l

from app import db, cache, celery
from app.activitypub.signature import post_request, default_context, send_post_request
from app.activitypub.util import find_actor_or_create, actor_json_to_model, \
    find_hashtag_or_create, create_post, remote_object_to_json, find_flair, activitypub_visibility, host_of
from app.community.forms import CreateLinkForm
from app.constants import SRC_WEB, POST_TYPE_LINK
from app.models import Community, File, PostReply, Post, utcnow, CommunityMember, Site, _as_dict, parse_ap_timestamp, \
    Instance, User, Tag, CommunityFlair, CommunityThemeAllowed, markdown_source, \
    language_from_ap, _as_url
from app.utils import get_request, gibberish, ensure_directory_exists, ap_datetime, instance_banned, get_task_session, \
    store_files_in_s3, guess_mime_type, patch_db_session, instance_allowed, get_setting, scale_gif, theme_list, \
    sanitize_svg, can_create_post, can_create_post_reply
from sqlalchemy import func, desc, text
import os
import boto3
from app.utils import allowlist_html, markdown_to_html, html_to_text
from app.visibility import is_open
from app.activitypub.util import find_language_or_create


allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.mpo', '.avif', '.svg']


def search_for_community(address: str, allow_fetch: bool = True) -> Community | None:
    if address.startswith('!'):
        # Exactly one '@', with something on each side of it. `split('@')`
        # alone raised `ValueError: not enough values to unpack` for `!name`
        # and `too many values to unpack` for `!a@b@c` -- and the addresses
        # reaching here are built from URL segments and search boxes, so
        # `/c/a@b@c/subscribe` was a 500 rather than a "no such community".
        parts = address[1:].split('@')
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        name, server = parts

        if get_setting('use_allowlist') and not instance_allowed(server):
            return None
        if instance_banned(server):
            return None

        if current_app.config['SERVER_NAME'] == server:
            profile_id = f"https://{server}/c/{name.lower()}"
            already_exists = db.session.query(Community).filter_by(ap_profile_id=profile_id, ap_id=None).first()
            return already_exists

        already_exists = db.session.query(Community).filter_by(ap_id=address[1:]).first()
        if already_exists:
            return already_exists
        elif not allow_fetch:
            return None

        # Look up the profile address of the community using WebFinger
        try:
            webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                         params={'resource': f"acct:{address[1:]}"})
        except httpx.HTTPError:
            sleep(randint(3, 10))
            try:
                webfinger_data = get_request(f"https://{server}/.well-known/webfinger",
                                            params={'resource': f"acct:{address[1:]}"})
            except httpx.HTTPError:
                return None

        if webfinger_data.status_code == 200:
            webfinger_json = webfinger_data.json()
            # D1397. Both of these are a REMOTE host's webfinger document, which
            # this function fetches from a hostname a caller supplied. `links` not
            # being a list was a TypeError, and a string element made
            # `'rel' in links` a substring test whose subscript raises.
            if not isinstance(webfinger_json, dict) or not isinstance(webfinger_json.get('links'), list):
                return None
            for links in webfinger_json['links']:
                links = _as_dict(links)
                if 'rel' in links and links['rel'] == 'self':  # this contains the URL of the activitypub profile
                    if 'href' not in links:  # as search_for_feed:80 already does
                        continue
                    type = links['type'] if 'type' in links else 'application/activity+json'
                    # retrieve the activitypub profile
                    community_data = get_request(links['href'], headers={'Accept': type})
                    # to see the structure of the json contained in community_data, do a GET to https://lemmy.world/c/technology with header Accept: application/activity+json
                    if community_data.status_code == 200:
                        try:
                            community_json = community_data.json()
                            community_data.close()
                        except:
                            community_data.close()
                            return None
                        if community_json['type'] == 'Group':
                            community = actor_json_to_model(community_json, name, server)
                            if community:
                                if current_app.debug:
                                    retrieve_mods_and_backfill(community.id, server, name, community_json)
                                else:
                                    retrieve_mods_and_backfill.delay(community.id, server, name, community_json)
                            return community
        return None


BACKFILL_ITEMS = 50
BACKFILL_MAX_PAGES = 10
BACKFILL_UNREADABLE = 'outbox unreadable'
BACKFILL_NO_OUTBOX = 'no outbox'


def _stored_object_id(announce, by_reference: bool):
    """The id of the post an outbox entry carries, without fetching it: the Announce's object url (PeerTube,
    a.gup.pe), the Announce's inner object's id (Lemmy), or a Create's object id (Castopod, WordPress)."""
    if not isinstance(announce, dict):
        return None
    obj = announce.get('object')
    if by_reference:
        return obj if isinstance(obj, str) else None
    if not isinstance(obj, dict):
        return None
    inner = obj.get('object')
    if isinstance(inner, str):
        return inner    # a Create whose object is a url
    if isinstance(inner, dict):
        return inner.get('id') if isinstance(inner.get('id'), str) else None
    return obj.get('id') if isinstance(obj.get('id'), str) else None


def _walk_outbox_pages(first):
    """Fetch outbox pages from `first` along `next` until BACKFILL_ITEMS items or
    BACKFILL_MAX_PAGES pages, and return the first page with every page's items,
    or None when the first page answers nothing."""
    items, seen, url, first_page = [], set(), first, None
    while isinstance(url, str) and url not in seen and len(seen) < BACKFILL_MAX_PAGES \
            and len(items) < BACKFILL_ITEMS:
        seen.add(url)
        page = remote_object_to_json(url)
        if not isinstance(page, dict):
            break
        if first_page is None:
            first_page = page
        page_items = page.get('orderedItems')
        if not isinstance(page_items, list):
            break
        items.extend(page_items)
        url = page.get('next')
    if first_page is None or not isinstance(first_page.get('orderedItems'), list):
        return first_page
    return dict(first_page, orderedItems=items)


@celery.task
def retrieve_mods_and_backfill(community_id: int, server, name, community_json=None, stop_at_known=False):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                community = session.get(Community, community_id)
                if not community:
                    return
                site = session.get(Site, 1)

                is_peertube = is_guppe = is_wordpress = False
                mod = None
                if community.ap_profile_id == f"https://{server}/video-channels/{name}":
                    is_peertube = True
                elif community.ap_profile_id.startswith('https://ovo.st/club'):
                    is_guppe = True

                # get mods
                if community.ap_moderators_url:
                    # PERM-5: moderator is a power on this instance, so only the
                    # community's own host may say who holds it.
                    if host_of(community.ap_moderators_url) != host_of(community.ap_profile_id):
                        current_app.logger.warning(f'Not reading moderators of {community.ap_profile_id} '
                                                   f'from another host: {community.ap_moderators_url}')
                        mods_data = None
                    else:
                        mods_data = remote_object_to_json(community.ap_moderators_url)
                    # This is whatever the remote instance sent. A collection
                    # without a `type` was a KeyError that killed the backfill task
                    # -- the community was created and then never filled in -- and
                    # an earlier round added a membership test for it.
                    #
                    # D1397 took both keys further, because a membership test is
                    # not a type check: `mods_data` itself may be a string or a
                    # list, and `orderedItems` being a string iterated its
                    # CHARACTERS into find_actor_or_create one at a time.
                    if (mods_data and isinstance(mods_data, dict) and mods_data.get('type') == 'OrderedCollection'
                            and isinstance(mods_data.get('orderedItems'), list)):
                        for actor in mods_data['orderedItems']:
                            sleep(0.5)
                            mod = find_actor_or_create(actor, retry=True)
                            if mod:
                                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id, user_id=mod.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = True
                                else:
                                    new_membership = CommunityMember(community_id=community.id, user_id=mod.id, is_moderator=True)
                                    session.add(new_membership)
                                try:
                                    session.commit()
                                except IntegrityError:
                                    session.rollback()

                elif community_json and 'attributedTo' in community_json:
                    mods = community_json['attributedTo']
                    if isinstance(mods, list):
                        for m in mods:
                            # D1397. `'type' in m` with no isinstance first, so a
                            # string element -- which is what `attributedTo` holds
                            # when a peer lists its moderators by url -- made this a
                            # substring test, and any url containing 'type'
                            # ('prototype', 'stereotype', '/type/1') then raised
                            # `TypeError: string indices must be integers` and ended
                            # the backfill. A url WITHOUT 'type' in it was silently
                            # skipped instead, though the two sibling readers of a
                            # list `attributedTo` (app/activitypub/util.py:4472,
                            # :4556) both accept one. Same shape as those two now.
                            if isinstance(m, str):
                                actor = m
                            elif isinstance(m, dict) and m.get('type') == 'Person':
                                actor = m.get('id')
                            else:
                                continue
                            if not isinstance(actor, str) or not actor:
                                continue
                            if host_of(actor) != host_of(community.ap_profile_id):  # PERM-5, as above
                                current_app.logger.warning(f'Not making {actor} a moderator of '
                                                           f'{community.ap_profile_id}: another host')
                                continue
                            mod = find_actor_or_create(actor, retry=True)
                            if mod:
                                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id, user_id=mod.id).first()
                                if existing_membership:
                                    existing_membership.is_moderator = True
                                else:
                                    new_membership = CommunityMember(community_id=community.id, user_id=mod.id, is_moderator=True)
                                    session.add(new_membership)
                                try:
                                    session.commit()
                                except IntegrityError:
                                    session.rollback()
                if is_peertube:
                    community.restricted_to_mods = True
                session.commit()

                # only backfill nsfw if nsfw communities are allowed
                if (community.nsfw and not site.enable_nsfw) or (community.nsfl and not site.enable_nsfl):
                    return

                # download 50 old posts (with Celery, or just 2 without). A paginated outbox is
                # walked from `first` along `next` until 50 items or BACKFILL_MAX_PAGES pages (D173 follow-up)
                outcome = None
                if community.ap_outbox_url:
                    outbox_data = remote_object_to_json(community.ap_outbox_url)
                    if not outbox_data:
                        return BACKFILL_UNREADABLE
                    if 'totalItems' in outbox_data and outbox_data['totalItems'] == 0:
                        return
                    if 'first' in outbox_data:
                        outbox_data = _walk_outbox_pages(outbox_data['first'])
                        if not outbox_data:
                            return BACKFILL_UNREADABLE
                    max = 50
                    if current_app.debug:
                        max = 2
                    if 'type' in outbox_data and (outbox_data['type'] == 'OrderedCollection' or outbox_data['type'] == 'OrderedCollectionPage') and 'orderedItems' in outbox_data:
                        activities_processed = 0
                        for announce in outbox_data['orderedItems']:
                            # D1397. Every read below treats this entry as an
                            # object. A string element made `'object' in announce` a
                            # substring test and the subscript a TypeError, which
                            # ended the backfill for the whole community.
                            announce = _as_dict(announce)
                            if stop_at_known:
                                known_id = _stored_object_id(announce, is_peertube or is_guppe)
                                if known_id and Post.get_by_ap_id(known_id) is not None:
                                    break   # outboxes are newest first: everything after this is here already
                            activity = None
                            if is_peertube or is_guppe:
                                # `.get`, as the branch below tests for: an
                                # Announce without an `object` was a KeyError
                                # here, and one malformed entry in a remote
                                # outbox stopped the whole backfill.
                                if 'object' not in announce:
                                    continue
                                activity = remote_object_to_json(announce['object'])
                            elif 'object' in announce and 'object' in _as_dict(announce['object']):
                                activity = announce['object']['object']
                            elif 'type' in announce and announce['type'] == 'Create':
                                activity = announce['object']
                                is_wordpress = True
                            if not activity:
                                continue    # `return` here threw away every
                                            # entry after a malformed one
                            if is_peertube and mod:
                                user = mod
                            elif 'attributedTo' in activity and isinstance(activity['attributedTo'], str):
                                user = find_actor_or_create(activity['attributedTo'], retry=True)
                                if not user:
                                    continue
                            else:
                                continue
                            if user.is_local():
                                continue
                            if not can_create_post(user, community):  # PERM-3: the inbound Create gate
                                current_app.logger.warning(f'Backfill of {community.ap_profile_id} skipped '
                                                           f'{activity.get("id")}: author may not post')
                                continue
                            if is_peertube or is_guppe:
                                request_json = {'id': f"https://{server}/activities/create/{gibberish(15)}", 'object': activity}
                            elif is_wordpress:
                                request_json = announce
                            else:
                                request_json = announce['object']
                            try:
                                post = create_post(True, community, request_json, user, announce['id'])
                            except Exception as e:
                                session.rollback()
                                # Log the error but continue processing other posts
                                print(f"Error creating post: {e}")
                                continue
                            if not session.is_active:
                                # create_post can swallow a failed flush (a PeerTube announce id longer than its
                                # column, say) and return None. Without this rollback the next entry's first query
                                # raised PendingRollbackError and ended the whole backfill with nothing stored.
                                session.rollback()
                                current_app.logger.warning(f'Backfill of {community.ap_profile_id} skipped '
                                                           f'{activity.get("id")}: it could not be stored')
                                continue
                            if post:
                                if 'published' in activity:
                                    # Post.new dated and ranked the post by its arrival; date and rank it by when it
                                    # was published, or a backfilled channel sorts by ingestion order (hot = ranking).
                                    # An unreadable date keeps the arrival date rather than reaching the column raw.
                                    published = parse_ap_timestamp(activity['published'])
                                    if published is not None:
                                        post.posted_at = published
                                        post.last_active = published
                                        post.ranking = post.post_ranking(post.score, published)
                                        post.ranking_scaled = int(post.ranking + community.scale_by())
                                    session.commit()

                                    # create post_replies based on activity['replies'], if it exists
                                    if 'replies' in activity and isinstance(activity['replies'], str):
                                        replies = remote_object_to_json(activity['replies'])
                                        if replies and 'type' in replies and replies['type'] == 'OrderedCollection' and 'orderedItems' in replies:
                                            for reply_data in replies['orderedItems']:
                                                # Everything below comes off the
                                                # wire: an entry with no id, and
                                                # one with no author, were each a
                                                # KeyError that killed the task.
                                                if not isinstance(reply_data, dict) or 'id' not in reply_data:
                                                    continue
                                                # Skip if reply already exists
                                                if session.query(PostReply).filter_by(ap_id=reply_data['id']).first():
                                                    continue

                                                # Refuse non-public replies, same policy as create_post_reply.
                                                # reply_data IS the object here (see the synthesised
                                                # reply_data['object'] below), so classify it directly rather
                                                # than a nested 'object' key that does not exist yet.
                                                if activitypub_visibility(reply_data) != 'public':
                                                    continue

                                                # Find the author of the reply
                                                if 'attributedTo' not in reply_data:
                                                    continue
                                                reply_author = find_actor_or_create(reply_data['attributedTo'], retry=True)
                                                if not reply_author:
                                                    continue
                                                if not can_create_post_reply(reply_author, community):  # PERM-3
                                                    current_app.logger.warning(f'Backfill of {community.ap_profile_id} skipped '
                                                                               f'{reply_data["id"]}: author may not reply')
                                                    continue
                                                
                                                # Extract reply content
                                                body = body_html = ''
                                                if 'content' in reply_data:
                                                    if not (reply_data['content'].startswith('<p>') or reply_data['content'].startswith('<blockquote>')):
                                                        reply_data['content'] = '<p>' + reply_data['content'] + '</p>'
                                                    body_html = allowlist_html(reply_data['content'])
                                                    source_markdown = markdown_source(reply_data)  # D1346
                                                    if source_markdown is not None:
                                                        body = source_markdown
                                                        body_html = markdown_to_html(body)
                                                    else:
                                                        body = html_to_text(body_html)
                                                
                                                # Find parent (post or comment this is replying to)
                                                in_reply_to = None
                                                if 'inReplyTo' in reply_data:
                                                    # Check if replying to the post itself
                                                    if reply_data['inReplyTo'] == post.ap_id:
                                                        in_reply_to = None  # Direct reply to post
                                                    else:
                                                        # Check if replying to another comment
                                                        parent_comment = session.query(PostReply).filter_by(ap_id=reply_data['inReplyTo']).first()
                                                        if parent_comment:
                                                            in_reply_to = parent_comment
                                                
                                                # Get language
                                                language_id = None
                                                ap_language = language_from_ap(reply_data.get('language'))  # D1355
                                                if ap_language is not None:
                                                    language = find_language_or_create(*ap_language,
                                                                                       session=session)
                                                    # A language this instance
                                                    # has not seen before is
                                                    # added and not flushed, so
                                                    # `language.id` was None and
                                                    # the reply kept no language
                                                    # at all.
                                                    session.flush()
                                                    language_id = language.id
                                                
                                                # Check if distinguished
                                                distinguished = reply_data.get('distinguished', False)
                                                answer = reply_data.get('answer', False)

                                                # Create the reply
                                                # D1406, as create_post_reply: this id
                                                # becomes PostReply.ap_id, which a
                                                # template renders as an href. This
                                                # tree came from a fetch, not an
                                                # inbox, so the check is repeated
                                                # here rather than inherited.
                                                if _as_url(reply_data.get('id')) is None:
                                                    continue
                                                try:
                                                    reply_data['object'] = {'id': reply_data['id']}
                                                    post_reply = PostReply.new(reply_author, post, in_reply_to, body, body_html,
                                                                               False, language_id, distinguished, answer, reply_data, session=session)
                                                    session.add(post_reply)
                                                    community.post_reply_count += 1
                                                    session.commit()
                                                except Exception as e:
                                                    session.rollback()
                                                    # Log the error but continue processing other replies
                                                    print(f"Error creating post reply: {e}")
                                                    continue
                            activities_processed += 1
                            if activities_processed >= max:
                                break
                        if community.post_count > 0:
                            newest = session.query(Post).filter(Post.community_id == community.id).order_by(desc(Post.posted_at)).first()
                            if newest:      # post_count is a counter, not a count
                                community.last_active = newest.posted_at
                                session.commit()
                else:
                    outcome = BACKFILL_NO_OUTBOX
                if community.ap_featured_url:
                    featured_data = remote_object_to_json(community.ap_featured_url)
                    if featured_data and 'type' in featured_data and featured_data['type'] == 'OrderedCollection' and 'orderedItems' in featured_data:
                        for item in featured_data['orderedItems']:
                            if not isinstance(item, dict) or 'id' not in item:
                                continue
                            post = session.query(Post).filter_by(ap_id=item['id']).first()
                            if post:
                                post.sticky = True
                                session.commit()
            session.execute(text("""UPDATE "post"
                                  SET reply_count = (
                                      SELECT COUNT(*)
                                      FROM post_reply
                                      WHERE post_reply.post_id = post.id
                                      AND post_reply.deleted = false
                                  )
                                  WHERE post.community_id = :community_id;
                                 """), {'community_id': community.id})
            session.commit()
            return outcome
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def actor_to_community(actor) -> Community:
    actor = actor.strip()
    if '@' in actor:
        community = Community.query.filter_by(banned=False, ap_id=actor).first()
    else:
        community = Community.query.filter(func.lower(Community.name) == func.lower(actor)).filter_by(banned=False, ap_id=None).first()
    return community


def end_poll_date(end_choice):
    delta_mapping = {
        '30m': timedelta(minutes=30),
        '1h': timedelta(hours=1),
        '6h': timedelta(hours=6),
        '12h': timedelta(hours=12),
        '1d': timedelta(days=1),
        '3d': timedelta(days=3),
        '7d': timedelta(days=7)
    }

    if end_choice in delta_mapping:
        return utcnow() + delta_mapping[end_choice]
    else:
        raise ValueError("Invalid choice")


def tags_from_string(tags: str) -> List[dict]:
    return_value = []
    tags = tags.strip()
    if tags == '':
        return []
    tag_list = tags.split(',')
    tag_list = [tag.strip() for tag in tag_list]
    for tag in tag_list:
        if tag == '':       # `news,` and `news,,sport` both produce one of
            continue        # these, and `tag[0]` on it was IndexError
        if tag[0] == '#':
            tag = tag[1:]
        tag_to_append = find_hashtag_or_create(tag)
        if tag_to_append:
            return_value.append({'type': 'Hashtag', 'name': tag_to_append.name})
    return return_value


def tags_from_string_old(tags: str) -> List[Tag]:
    return_value = []
    if tags is None:
        return []
    tags = tags.strip()
    if tags == '':
        return []
    if tags[-1:] == ',':
        tags = tags[:-1]
    tag_list = tags.split(',')
    tag_list = [tag.strip() for tag in tag_list]
    seen = set()
    for tag in tag_list:
        if tag == '':       # one trailing comma is stripped above; `,news`,
            continue        # `news,,sport` and `,` are not, and `tag[0]` on
                            # the empty tag they produce was IndexError
        if tag[0] == '#':
            tag = tag[1:]
        # Deduplicate by NAME. `tag_to_append not in return_value` compared
        # objects, and `find_hashtag_or_create` queries without flushing, so
        # the second `news` in `news,news` was a second pending Tag row --
        # `Tag.name` carries an index but no unique constraint, so both were
        # written and the tag existed twice.
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tag_to_append = find_hashtag_or_create(tag)
        if tag_to_append and tag_to_append not in return_value:
            return_value.append(tag_to_append)
    return return_value


def flair_from_form(tag_ids) -> List[CommunityFlair]:
    if tag_ids is None:
        return []
    return CommunityFlair.query.filter(CommunityFlair.id.in_(tag_ids)).all()


def flairs_from_string(flairs: str, community_id: int) -> List[Tag]:
    return_value = []
    if flairs is None:
        return []
    flairs = flairs.strip()
    if flairs == '':
        return []
    if flairs[-1:] == ',':
        flairs = flairs[:-1]
    flair_list = flairs.split(',')
    flair_list = [tag.strip() for tag in flair_list]
    for f in flair_list:
        flair_to_append = find_flair(f, community_id)
        if flair_to_append and flair_to_append not in return_value:
            return_value.append(flair_to_append)
    return return_value


def delete_post_from_community(post_id):
    if current_app.debug:
        delete_post_from_community_task(post_id, current_user.id)
    else:
        delete_post_from_community_task.delay(post_id, current_user.id)


@celery.task
def delete_post_from_community_task(post_id, user_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = session.get(User, user_id)
                post = session.get(Post, post_id)
                community = post.community
                post.deleted = True
                post.deleted_by = user.id
                session.commit()

                # E9: a followers-only post never reached a local community's followers; a remote community, its
                # home, is still sent the Delete (R-a)
                if not community.local_only and (is_open(post) or not community.is_local()):
                    delete_json = {
                        'id': f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}",
                        'type': 'Delete',
                        'actor': user.public_url(),
                        'audience': post.community.public_url(),
                        'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
                        'published': ap_datetime(utcnow()),
                        'cc': [
                            user.followers_url()
                        ],
                        'object': post.ap_id,
                    }

                    if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                        send_post_request(post.community.ap_inbox_url, delete_json, user.private_key, user.public_url() + '#main-key')
                    else:  # local community - send it to followers on remote instances
                        announce = {
                            "id": f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}",
                            "type": 'Announce',
                            "to": [
                                "https://www.w3.org/ns/activitystreams#Public"
                            ],
                            "actor": post.community.ap_profile_id,
                            "cc": [
                                post.community.ap_followers_url
                            ],
                            '@context': default_context(),
                            'object': delete_json
                        }

                        for instance in post.community.following_instances():
                            # `user`, not `current_user`. This runs in a Celery
                            # worker, where there is no request and
                            # `current_user` is None, so the announce loop was
                            # an AttributeError -- the post was already marked
                            # deleted and committed, and the delete never
                            # federated. The sibling task below names the
                            # author for the same reason.
                            if instance.inbox and not user.has_blocked_instance(instance.id) and not instance_banned(
                                    instance.domain):
                                send_to_remote_instance(instance.id, post.community.id, announce)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def delete_post_reply_from_community(post_reply_id, user_id):
    if current_app.debug:
        delete_post_reply_from_community_task(post_reply_id, user_id)
    else:
        delete_post_reply_from_community_task.delay(post_reply_id, user_id)


@celery.task
def delete_post_reply_from_community_task(post_reply_id, user_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = session.get(User, user_id)
                post_reply = session.get(PostReply, post_reply_id)
                post = post_reply.post

                post_reply.deleted = True
                post_reply.deleted_by = user.id
                session.commit()

                # federate delete (E9: not to a local community's followers for a followers-only reply, which never
                # reached them; a remote community, its home, is still sent it -- R-a)
                if not post.community.local_only and (is_open(post_reply) or not post.community.is_local()):
                    delete_json = {
                        'id': f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}",
                        'type': 'Delete',
                        'actor': user.public_url(),
                        'audience': post.community.public_url(),
                        'to': [post.community.public_url(), 'https://www.w3.org/ns/activitystreams#Public'],
                        'published': ap_datetime(utcnow()),
                        'cc': [
                            user.followers_url()
                        ],
                        'object': post_reply.ap_id,
                    }

                    if not post.community.is_local():  # this is a remote community, send it to the instance that hosts it
                        send_post_request(post.community.ap_inbox_url, delete_json, user.private_key, user.public_url() + '#main-key')

                    else:  # local community - send it to followers on remote instances
                        announce = {
                            "id": f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}",
                            "type": 'Announce',
                            "to": [
                                "https://www.w3.org/ns/activitystreams#Public"
                            ],
                            "actor": post.community.ap_profile_id,
                            "cc": [
                                post.community.ap_followers_url
                            ],
                            '@context': default_context(),
                            'object': delete_json
                        }

                        for instance in post.community.following_instances():
                            if instance.inbox and not post_reply.author.has_blocked_instance(instance.id) \
                                    and not instance_banned(instance.domain):
                                send_to_remote_instance(instance.id, post.community.id, announce)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def remove_old_file(file_id):
    remove_file = db.session.get(File, file_id)
    if remove_file:     # the row can be gone by the time the caller gets here
        remove_file.delete_from_disk()


def save_icon_file(icon_file, directory='communities') -> File:
    # check if this is an allowed type of file
    file_ext = os.path.splitext(icon_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        abort(400)
    new_filename = gibberish(15)

    # set up the storage directory
    if store_files_in_s3():
        local_directory = 'app/static/tmp'
    else:
        local_directory = f'app/static/media/{directory}/{new_filename[0:2]}/{new_filename[2:4]}'
    ensure_directory_exists(local_directory)

    # save the file
    s3_directory = f'{directory}/{new_filename[0:2]}/{new_filename[2:4]}'
    final_place = os.path.join(local_directory, new_filename + file_ext)
    final_place_thumbnail = os.path.join(local_directory, new_filename + '_thumbnail.webp')
    icon_file.save(final_place)

    # An SVG that cannot be sanitized is rejected. The '.svg' branch below skips
    # the Pillow re-encode ("svgs don't need to be resized"), so nothing
    # downstream would repair or re-check the file -- it would be served from
    # this site's own origin exactly as uploaded. sanitize_svg has already
    # destroyed the file by the time it returns False. abort(400) is this
    # function's own convention for an upload it will not accept, the same as
    # the extension check above and the trailing else below.
    if file_ext.lower() == '.svg' and not sanitize_svg(final_place):
        abort(400)

    if file_ext.lower() in ('.heic', '.heif'):  # D579: two names for one format
        register_heif_opener()
    elif file_ext.lower() == '.avif':
        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

    # resize if necessary or if using MEDIA_IMAGE_FORMAT
    # (the extension check at the top of this function already aborts for anything not in allowed_extensions)
    # Process the image based on file type
    if file_ext.lower() == '.svg':  # svgs don't need to be resized
        img_width = None
        img_height = None
        thumbnail_width = None
        thumbnail_height = None
        final_ext = file_ext.lower()
        thumbnail_ext = file_ext.lower()
        final_place_thumbnail = final_place
    elif file_ext.lower() == '.gif':  # handle animated gifs specially
        img = Image.open(final_place)
        img_width = img.width
        img_height = img.height

        # Use scale_gif for resizing animated GIFs
        if img.width > 250 or img.height > 250:
            scale_gif(final_place, (250, 250))
            img = Image.open(final_place)
            img_width = img.width
            img_height = img.height

        # Create thumbnail
        final_ext = file_ext.lower()
        thumbnail_ext = '.gif'
        final_place_thumbnail = os.path.join(local_directory, new_filename + '_thumbnail.gif')
        scale_gif(final_place, (40, 40), final_place_thumbnail)
        img_thumb = Image.open(final_place_thumbnail)
        thumbnail_width = img_thumb.width
        thumbnail_height = img_thumb.height
    else:  # handle regular images (jpg, png, webp, heic, etc.)
        img = Image.open(final_place)
        img = ImageOps.exif_transpose(img)
        img_width = img.width
        img_height = img.height

        image_format = current_app.config['MEDIA_IMAGE_FORMAT']
        image_quality = current_app.config['MEDIA_IMAGE_QUALITY']
        thumbnail_image_format = current_app.config['MEDIA_IMAGE_THUMBNAIL_FORMAT']
        thumbnail_image_quality = current_app.config['MEDIA_IMAGE_THUMBNAIL_QUALITY']

        final_ext = file_ext.lower()
        thumbnail_ext = file_ext.lower()

        if image_format == 'AVIF' or thumbnail_image_format == 'AVIF':
            import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

        if img.width > 250 or img.height > 250 or image_format or thumbnail_image_format:
            img = img.convert('RGB' if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')
            img.thumbnail((250, 250), resample=Image.LANCZOS)

            kwargs = {}
            if image_format:
                kwargs['format'] = image_format.upper()
                final_ext = '.' + image_format.lower()
                final_place = os.path.splitext(final_place)[0] + final_ext
            if image_quality:
                kwargs['quality'] = int(image_quality)
            img.save(final_place, optimize=True, **kwargs)

            img_width = img.width
            img_height = img.height
        # save a second, smaller, version as a thumbnail
        img = img.convert('RGB' if thumbnail_image_format == 'JPEG' else 'RGBA')
        img.thumbnail((40, 40), resample=Image.LANCZOS)

        kwargs = {}
        if thumbnail_image_format:
            kwargs['format'] = thumbnail_image_format.upper()
            thumbnail_ext = '.' + thumbnail_image_format.lower()
            final_place_thumbnail = os.path.splitext(final_place_thumbnail)[0] + thumbnail_ext
        if thumbnail_image_quality:
            kwargs['quality'] = int(thumbnail_image_quality)
        img.save(final_place_thumbnail, optimize=True, **kwargs)

        thumbnail_width = img.width
        thumbnail_height = img.height

    # Create the File object
    file = File(file_path=final_place, file_name=new_filename + final_ext, alt_text=f'{directory} icon',
                width=img_width, height=img_height, thumbnail_width=thumbnail_width,
                thumbnail_height=thumbnail_height, thumbnail_path=final_place_thumbnail)
    db.session.add(file)

    # Move uploaded files to S3 if needed
    if store_files_in_s3():
        session = boto3.session.Session()
        s3 = session.client(
            service_name='s3',
            region_name=current_app.config['S3_REGION'],
            endpoint_url=current_app.config['S3_ENDPOINT'],
            aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
            aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
        )
        # Upload main image
        s3_path = f'{s3_directory}/{new_filename}{final_ext}'
        extra_args = {'ContentType': guess_mime_type(final_place)}
        if current_app.config.get('S3_STORAGE_CLASS'):
            extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
        if current_app.config.get('S3_PUBLIC_ACL'):
            extra_args['ACL'] = 'public-read'
        s3.upload_file(final_place, current_app.config['S3_BUCKET'], s3_path, ExtraArgs=extra_args)
        file.file_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{s3_path}"

        # Upload thumbnail (if different from main image)
        if final_place_thumbnail != final_place:
            s3_thumbnail_path = f'{s3_directory}/{new_filename}_thumbnail{thumbnail_ext}'
            extra_args = {'ContentType': guess_mime_type(final_place_thumbnail)}
            if current_app.config.get('S3_STORAGE_CLASS'):
                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
            if current_app.config.get('S3_PUBLIC_ACL'):
                extra_args['ACL'] = 'public-read'
            s3.upload_file(final_place_thumbnail, current_app.config['S3_BUCKET'], s3_thumbnail_path, ExtraArgs=extra_args)
            file.thumbnail_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{s3_thumbnail_path}"
            os.unlink(final_place_thumbnail)
        else:
            file.thumbnail_path = file.file_path

        s3.close()
        os.unlink(final_place)

    return file
    return file


def save_banner_file(banner_file, directory='communities') -> File:
    # check if this is an allowed type of file
    file_ext = os.path.splitext(banner_file.filename)[1]
    if file_ext.lower() not in allowed_extensions:
        abort(400)
    new_filename = gibberish(15)

    # set up the storage directory
    if store_files_in_s3():
        local_directory = 'app/static/tmp'
    else:
        local_directory = f'app/static/media/{directory}/{new_filename[0:2]}/{new_filename[2:4]}'
    ensure_directory_exists(local_directory)

    # save the file or if using MEDIA_IMAGE_FORMAT
    s3_directory = f'{directory}/{new_filename[0:2]}/{new_filename[2:4]}'
    final_place = os.path.join(local_directory, new_filename + file_ext)
    final_place_thumbnail = os.path.join(local_directory, new_filename + '_thumbnail.webp')
    banner_file.save(final_place)

    # No SVG banner, clean or otherwise. '.svg' is in allowed_extensions, which
    # is shared with save_icon_file, and THAT function has an '.svg' branch that
    # skips the Pillow work. This one does not, so every SVG banner reached
    # `Image.open` and raised UnidentifiedImageError: a 500, with the uploaded
    # bytes left sitting in the media root and nothing to clean them up. A
    # refusal is the answer, and it is the same 400 the extension check above
    # gives. The sanitize_svg call stays ahead of it so that a hostile SVG is
    # destroyed on the way past rather than merely refused.
    if file_ext.lower() == '.svg':
        sanitize_svg(final_place)
        os.unlink(final_place) if os.path.exists(final_place) else None
        abort(400)

    if file_ext.lower() in ('.heic', '.heif'):  # D579: two names for one format
        register_heif_opener()
    elif file_ext.lower() == '.avif':
        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

    # resize if necessary
    img = Image.open(final_place)
    # Pillow names the format of a .heic file HEIF, and the allowlist spells
    # it .heic -- so a HEIC banner failed this check and was refused with a
    # 400, though .heic is an allowed extension and save_icon_file takes one.
    img_ext = '.heic' if img.format == 'HEIF' else '.' + img.format.lower()
    if img_ext in allowed_extensions:
        img = ImageOps.exif_transpose(img)

        image_format = current_app.config['MEDIA_IMAGE_FORMAT']
        image_quality = current_app.config['MEDIA_IMAGE_QUALITY']
        thumbnail_image_format = current_app.config['MEDIA_IMAGE_THUMBNAIL_FORMAT']
        thumbnail_image_quality = current_app.config['MEDIA_IMAGE_THUMBNAIL_QUALITY']

        final_ext = file_ext.lower()
        thumbnail_ext = file_ext.lower()
        img_width = img.width
        img_height = img.height

        if image_format == 'AVIF' or thumbnail_image_format == 'AVIF':
            import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path

        if img.width > 1600 or img.height > 600 or image_format or thumbnail_image_format:
            img = img.convert('RGB' if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']) else 'RGBA')
            img.thumbnail((1600, 600), resample=Image.LANCZOS)

            kwargs = {}
            if image_format:
                kwargs['format'] = image_format.upper()
                final_ext = '.' + image_format.lower()
                final_place = os.path.splitext(final_place)[0] + final_ext
            if image_quality:
                kwargs['quality'] = int(image_quality)
            img.save(final_place, optimize=True, **kwargs)

            img_width = img.width
            img_height = img.height

        # save a second, smaller, version as a thumbnail
        img = img.convert('RGB' if thumbnail_image_format == 'JPEG' else 'RGBA')
        img.thumbnail((878, 500), resample=Image.LANCZOS)

        kwargs = {}
        if thumbnail_image_format:
            kwargs['format'] = thumbnail_image_format.upper()
            thumbnail_ext = '.' + thumbnail_image_format.lower()
            final_place_thumbnail = os.path.splitext(final_place_thumbnail)[0] + thumbnail_ext
        if thumbnail_image_quality:
            kwargs['quality'] = int(thumbnail_image_quality)
        img.save(final_place_thumbnail, optimize=True, **kwargs)

        thumbnail_width = img.width
        thumbnail_height = img.height

        file = File(file_path=final_place, file_name=new_filename + final_ext, alt_text=f'{directory} banner',
                    width=img_width, height=img_height, thumbnail_path=final_place_thumbnail,
                    thumbnail_width=thumbnail_width, thumbnail_height=thumbnail_height)
        db.session.add(file)
        
        # Move uploaded files to S3 if needed
        if store_files_in_s3():
            session = boto3.session.Session()
            s3 = session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            # Upload main image
            s3_path = f'{s3_directory}/{new_filename}{final_ext}'
            extra_args = {'ContentType': guess_mime_type(final_place)}
            if current_app.config.get('S3_STORAGE_CLASS'):
                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
            if current_app.config.get('S3_PUBLIC_ACL'):
                extra_args['ACL'] = 'public-read'
            s3.upload_file(final_place, current_app.config['S3_BUCKET'], s3_path, ExtraArgs=extra_args)
            file.file_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{s3_path}"
            
            # Upload thumbnail
            s3_thumbnail_path = f'{s3_directory}/{new_filename}_thumbnail{thumbnail_ext}'
            extra_args = {'ContentType': guess_mime_type(final_place_thumbnail)}
            if current_app.config.get('S3_STORAGE_CLASS'):
                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
            if current_app.config.get('S3_PUBLIC_ACL'):
                extra_args['ACL'] = 'public-read'
            s3.upload_file(final_place_thumbnail, current_app.config['S3_BUCKET'], s3_thumbnail_path, ExtraArgs=extra_args)
            file.thumbnail_path = f"https://{current_app.config['S3_PUBLIC_URL']}/{s3_thumbnail_path}"
            
            s3.close()
            os.unlink(final_place)
            os.unlink(final_place_thumbnail)
            
        return file
    else:
        abort(400)


def set_community_theme_allowed(community_id: int, user_id: int, allowed:bool):
    cache.delete_memoized(get_community_theme_allowed,community_id,user_id)
    community_theme_allowed = CommunityThemeAllowed.query.filter_by(community_id=community_id,user_id=user_id).first()
    if community_theme_allowed is None:
        theme_allowed = CommunityThemeAllowed(community_id=community_id, user_id=user_id, allowed=allowed)
        db.session.add(theme_allowed)
    else:
        community_theme_allowed.allowed = allowed
    db.session.commit()

@cache.memoize(timeout=86400)
def get_community_theme_allowed(community_id: int, user_id: int) ->bool:
    community_theme_allowed = CommunityThemeAllowed.query.filter_by(community_id=community_id,user_id=user_id).first()
    if community_theme_allowed is None:
        return True
    return community_theme_allowed.allowed


# NB this always signs POSTs as the community so is only suitable for Announce activities
def send_to_remote_instance(instance_id: int, community_id: int, payload):
    if current_app.debug:
        send_to_remote_instance_task(instance_id, community_id, payload)
    else:
        send_to_remote_instance_task.delay(instance_id, community_id, payload)


@celery.task
def send_to_remote_instance_task(instance_id: int, community_id: int, payload):
    session = get_task_session()
    try:
        community: Community = session.get(Community, community_id)
        if community:
            instance: Instance = session.get(Instance, instance_id)
            if instance and instance.inbox and instance.online() and not instance_banned(instance.domain):
                send_post_request(instance.inbox, payload, community.private_key, community.ap_profile_id + '#main-key',
                                  timeout=10, new_task=False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def send_to_remote_instance_fast(inbox: str, community_private_key: str, community_ap_profile_id: str, payload):
    # a faster version of send_to_remote_instance that does not use the DB
    if current_app.debug:
        send_to_remote_instance_fast_task(inbox, community_private_key, community_ap_profile_id, payload)
    else:
        send_to_remote_instance_fast_task.delay(inbox, community_private_key, community_ap_profile_id, payload)


@celery.task
def send_to_remote_instance_fast_task(inbox: str, community_private_key: str, community_ap_profile_id: str, payload):
    send_post_request(inbox, payload, community_private_key, community_ap_profile_id + '#main-key',
                      timeout=10, new_task=False)


def community_in_list(community_id, community_list):
    for tup in community_list:
        if community_id == tup[0]:
            return True
    return False


def find_local_users(search: str) -> List[User]:
    return User.query.filter(User.banned == False, User.deleted == False, User.ap_id == None, User.user_name.ilike(f"%{search}%")).\
        order_by(desc(User.reputation)).all()


def find_potential_moderators(search: str) -> List[User]:
    if not '@' in search:
        return User.query.filter(User.banned == False, User.deleted == False, User.user_name.ilike(f"%{search}%")).\
          order_by(desc(User.reputation)).all()
    else:
        return User.query.filter(User.banned == False, User.deleted == False, User.ap_id == search.lower()).\
          order_by(desc(User.reputation)).all()


def hashtags_used_in_community(community_id: int, content_filters):
    tags = db.session.execute(text("""SELECT t.*, COUNT(post.id) AS pc
    FROM "tag" AS t
    INNER JOIN post_tag pt ON t.id = pt.tag_id
    INNER JOIN "post" ON pt.post_id = post.id
    WHERE post.community_id = :community_id
      AND t.banned IS FALSE AND post.deleted IS FALSE
    GROUP BY t.id
    ORDER BY pc DESC
    LIMIT 30;"""), {'community_id': community_id}).mappings().all()

    def tag_blocked(tag):
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in tag['name'].lower():
                    return True
        return False

    return normalize_font_size([dict(row) for row in tags if not tag_blocked(row)])


def hashtags_used_in_communities(community_ids: List[int], content_filters):
    if community_ids is None or len(list(community_ids)) == 0:
        return None
    tags = db.session.execute(text("""SELECT t.*, COUNT(post.id) AS pc
    FROM "tag" AS t
    INNER JOIN post_tag pt ON t.id = pt.tag_id
    INNER JOIN "post" ON pt.post_id = post.id
    WHERE post.community_id IN :community_ids
      AND t.banned IS FALSE AND post.deleted IS FALSE
    GROUP BY t.id
    ORDER BY pc DESC
    LIMIT 30;"""), {'community_ids': tuple(community_ids)}).mappings().all()

    def tag_blocked(tag):
        for name, keywords in content_filters.items() if content_filters else {}:
            for keyword in keywords:
                if keyword in tag['name'].lower():
                    return True
        return False

    return normalize_font_size([dict(row) for row in tags if not tag_blocked(row)])


def normalize_font_size(tags: List[dict], min_size=12, max_size=24):
    # Add a font size to each dict, based on the number of times each tag is used (the post count aka 'pc')
    if len(tags) == 0:
        return []
    pcs = [tag['pc'] for tag in tags]       # pcs = a list of all post counts. Sorry about the 'pc', the SQL that generates this dict had a naming collision
    min_pc, max_pc = min(pcs), max(pcs)

    def scale(pc):
        if max_pc == min_pc:
            return (min_size + max_size) // 2   # if all tags have the same count
        return min_size + (pc - min_pc) * (max_size - min_size) / (max_pc - min_pc)

    for tag in tags:
        tag['font_size'] = round(scale(tag['pc']), 1)   # add a font size based on its post count

    return tags


def publicize_community(community: Community):
    from app.shared.post import make_post  # cycle: app.shared.post imports tags_from_string_old from this module
    form = CreateLinkForm()
    form.title.data = community.title
    form.link_url.data = community.public_url()
    form.body.data = f'{community.lemmy_link()}\n\n'
    form.body.data += community.description if community.description else ''
    form.language_id.data = current_user.language_id or g.site.language_id
    if current_app.debug:
        community = Community.query.filter(Community.ap_id == 'playground@piefed.social').first()
    else:
        community = Community.query.filter(Community.ap_id == 'newcommunities@lemmy.world').first()

    if community:
        make_post(form, community, POST_TYPE_LINK, SRC_WEB)

        """
        Have this removed for now, due to several problems:

        1. 'community' has been over-ridden, and is now 'playground@piefed.social' or 'newcommunities@lemmy.world'

        2. Lemmy's v3/resolve_object endpoint doesn't accept queries in '!community@domain' format,
           it needs to be 'q={community.public_url()}'

        3. None of those instances will add data to their DBs based on an anonymous query, so will just respond 400
           to any communities they don't already know about

        if current_app.debug:
            publicize_community_task(community.id)
        else:
            publicize_community_task.delay(community.id)
        """


@celery.task
def publicize_community_task(community_id: int):
    session = get_task_session()
    try:
        community = session.get(Community, community_id)
        if community is None:
            return
        get_request(f'https://lemmy.world/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://sh.itjust.works/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://lemmy.zip/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://feddit.org/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://lemmy.dbzer0.com/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://lemmy.ca/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://lemmy.blahaj.zone/api/v3/resolve_object?q={community.lemmy_link()}')
        get_request(f'https://programming.dev/api/v3/resolve_object?q={community.lemmy_link()}')
    finally:
        session.close()


def is_bad_name(community_name: str) -> bool:
    name_lower = community_name.lower()
    # sort out the 'seven things you can't say on tv' names (cursewords), plus some "low effort" communities
    seven_things_plus = [
        'shit', 'piss', 'fuck',
        'cunt', 'cocksucker', 'motherfucker', 'tits',
        'greentext', '4chan', 'fauxbait'
    ]
    return any(badword in name_lower for badword in seven_things_plus)

def community_theme_list():
    community_themes = theme_list()
    community_themes.insert(0,('disabled', _l('Disabled')))
    return community_themes
