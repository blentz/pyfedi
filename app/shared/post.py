from __future__ import annotations

import contextlib
import os
from typing import List
from zoneinfo import ZoneInfo
from datetime import datetime

import boto3
from PIL import Image, ImageOps
from flask import flash, request, current_app, g, abort
from flask_babel import _, force_locale, gettext
from flask_login import current_user
from pillow_heif import register_heif_opener
from sqlalchemy import text, Integer
from sqlalchemy.orm.exc import NoResultFound

from app import db, cache, plugins, limiter
from app.activitypub.serializers.microblog import body_hashtags
from app.activitypub.util import find_hashtag_or_create, make_image_sizes, notify_about_post, host_of
from app.community.util import tags_from_string_old, end_poll_date, flair_from_form, flairs_from_string
from app.constants import *
from app.federation_urls import object_base
from app.models import File, Notification, NotificationSubscription, Poll, PollChoice, PollChoiceVote, Post, post_file, \
    PostBookmark, PostVote, Report, Site, User, utcnow, Instance, Event, Community, CommunityFlair, \
    votes_cast_today, adjust_domain_post_count, served_path, _as_url
from app.shared.media import MediaRefused, can_upload_media, episode_form_wanted, media_kind_for, media_of, \
    queue_probe, store_uploaded_media, with_episode
from app.local_only import content_local_only
from app.onion import apply_audit, is_onion_request, stamp_activity
from app.onion_account import audit_ip
from app.storefront import is_upload_link, link_on, stored_link
from app.shared.tasks import task_selector
from app.shared.upload import open_uploaded_image
from app.visibility import is_open, post_title_for
from app.utils import render_template, authorise_api_user, shorten_string, gibberish, ensure_directory_exists, \
    max_album_images, \
    piefed_markdown_to_lemmy_markdown, markdown_to_html, fixup_url, domain_from_url, \
    opengraph_parse, url_to_thumbnail_file, can_create_post, is_video_hosting_site, recently_upvoted_posts, \
    is_image_url, add_to_modlog, store_files_in_s3, guess_mime_type, retrieve_image_hash, \
    hash_matches_blocked_image, can_upvote, can_downvote, get_recipient_language, to_srgb, \
    is_video_url, sanitize_svg, user_ip_banned, ip_address, inspect_image_c2pa, save_image_without_metadata, \
    community_membership_private, is_local_microblogs, communities_banned_from, can_moderate, can_create_post_reply
import app as app_pkg


def vote_for_post(post_id: int, vote_direction, federate: bool, emoji: str, src, auth=None):
    if src == SRC_API:
        post = db.session.get(Post, post_id) or abort(404)
        user = authorise_api_user(auth, return_type='model')
        if vote_direction == 'upvote' and not can_upvote(user, post.community):
            return user.id
        elif vote_direction == 'downvote' and not can_downvote(user, post.community):
            return user.id
        elif vote_direction == 'reversal':
            # The twin of app/shared/reply.py's gate, and it moves with it. 'reversal'
            # names no permission of its own, so the gate is the permission that would
            # have cast the vote being undone. app/api/alpha/utils/post.py maps any
            # score that is not +/-1 to 'reversal' exactly as the reply API does.
            existing_vote = db.session.query(PostVote).filter_by(
                user_id=user.id, post_id=post_id).first()
            if existing_vote:
                if existing_vote.effect > 0 and not can_upvote(user, post.community):
                    return user.id
                if existing_vote.effect < 0 and not can_downvote(user, post.community):
                    return user.id
    else:
        post = db.session.get(Post, post_id) or abort(404)
        user = current_user

        refused = (vote_direction == 'upvote' and not can_upvote(user, post.community)) or (
                vote_direction == 'downvote' and not can_downvote(user, post.community))
        if vote_direction == 'reversal':
            # The API arm's reversal gate above, for the URL's direction segment (D408).
            existing_vote = db.session.query(PostVote).filter_by(
                user_id=user.id, post_id=post_id).first()
            if existing_vote:
                refused = (existing_vote.effect > 0 and not can_upvote(user, post.community)) or (
                        existing_vote.effect < 0 and not can_downvote(user, post.community))
        if refused:
            template = 'post/_post_voting_buttons.html' if request.args.get('style',
                                                                            '') == '' else 'post/_post_voting_buttons_masonry.html'
            return render_template(template, post=post, community=post.community, recently_upvoted=[],
                                   recently_downvoted=[])

    if user.banned or user_ip_banned():
        abort(403)

    if votes_cast_today(user.id) >= current_app.config['VOTE_QUOTA']:
        abort(429)

    mark_post_read([post.id], True, user.id)

    undo = post.vote(user, vote_direction, emoji)

    task_selector('vote_for_post', user_id=user.id, post_id=post_id, vote_to_undo=undo, vote_direction=vote_direction, federate=federate, emoji=emoji)

    if src == SRC_API:
        return user.id
    else:
        recently_upvoted = []
        recently_downvoted = []
        if vote_direction == 'upvote' and undo is None:
            recently_upvoted = [post_id]
        elif vote_direction == 'downvote' and undo is None:
            recently_downvoted = [post_id]

        template = 'post/_post_voting_buttons.html' if request.args.get('style', '') == '' else 'post/_post_voting_buttons_masonry.html'
        return render_template(template, post=post, community=post.community, recently_upvoted=recently_upvoted,
                               recently_downvoted=recently_downvoted)


def bookmark_post(post_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    # D1125. `mark_post_read` INSERTs into `read_posts`, whose `read_post_id`
    # is a foreign key, so an id that does not resolve was
    # `psycopg2.errors.ForeignKeyViolation: insert or update on table
    # "read_posts" violates foreign key constraint` -- a 500, although the web
    # route already catches `NoResultFound` to answer 404 and the API arm
    # expects the same. The write ran BEFORE anything checked the row exists.
    if db.session.get(Post, post_id) is None:
        raise NoResultFound

    mark_post_read([post_id], True, user_id)

    existing_bookmark = PostBookmark.query.filter_by(post_id=post_id, user_id=user_id).first()
    if not existing_bookmark:
        db.session.add(PostBookmark(post_id=post_id, user_id=user_id))
        db.session.commit()
    else:
        msg = 'This post has already been bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def remove_bookmark_post(post_id: int, src, auth=None):
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    # D1125's other half: the web route catches `NoResultFound` to answer 404,
    # and nothing here ever raised it, so an id that does not resolve got 200
    # and the message "This post was not bookmarked." -- which is true of every
    # post that does not exist, and says nothing about the one asked for.
    if db.session.get(Post, post_id) is None:
        raise NoResultFound

    existing_bookmark = PostBookmark.query.filter_by(post_id=post_id, user_id=user_id).first()
    if existing_bookmark:
        db.session.delete(existing_bookmark)
        db.session.commit()
    else:
        msg = 'This post was not bookmarked.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg))

    if src == SRC_API:
        return user_id


def subscribe_post(post_id: int, subscribe, src, auth=None):
    post = db.session.query(Post).filter_by(id=post_id, deleted=False).one()
    user_id = authorise_api_user(auth) if src == SRC_API else current_user.id

    if src == SRC_WEB:
        subscribe = False if post.notify_new_replies(user_id) else True

    existing_notification = NotificationSubscription.query.filter_by(entity_id=post_id, user_id=user_id,
                                                                     type=NOTIF_POST).first()
    if subscribe == False:
        if existing_notification:
            db.session.delete(existing_notification)
            db.session.commit()
        else:
            msg = 'A subscription for this post did not exist.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))

    else:
        if existing_notification:
            msg = 'A subscription for this post already existed.'
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg))
        else:
            new_notification = NotificationSubscription(
                name=shorten_string(_('Replies to my post %(post_title)s', post_title=post.title)),
                user_id=user_id, entity_id=post_id, type=NOTIF_POST)
            db.session.add(new_notification)
            db.session.commit()

    if src == SRC_API:
        return user_id
    else:
        return render_template('post/_post_notification_toggle.html', post=post)


def extra_rate_limit_check(user):
    """
    The plan for this function is to do some extra limiting for an author who passes the rate limit for the route
    but who's posts are really unpopular and are probably spam
    """
    return False


def requested_visibility(input, src, community) -> str:
    """The visibility a new local post asks for (Amendment B, S7.1).

    Only the local microblogs community takes 'followers'. A value that is absent or empty means public. Anything
    else, or 'followers' anywhere else, is refused, before any row is written."""
    if src == SRC_API:
        visibility = input.get('visibility') or VISIBILITY_PUBLIC
    else:
        field = getattr(input, 'visibility', None)     # a form object without the field means public
        visibility = (field.data if field is not None else None) or VISIBILITY_PUBLIC
    if visibility not in (VISIBILITY_PUBLIC, VISIBILITY_FOLLOWERS):
        raise Exception('invalid visibility')
    if visibility == VISIBILITY_FOLLOWERS and not is_local_microblogs(community):
        raise Exception('invalid visibility')
    return visibility


# Tag.name and Tag.display_as are String(256): a longer body #word is linked in the Note but not stored as a tag.
TAG_NAME_MAX = 256


def with_body_hashtags(tags: list, body_html) -> list:
    """`tags` and then each `#word` of `body_html` that is not among them, as stored Tags (spec 2026-10-14 §10 A12).
    The words are the ones the Note links in its content (link_hashtags), so what is linked is what is tagged.
    A new Tag keeps the first spelling used; matching is case-blind."""
    names = {tag.name for tag in tags}
    for name, spelling in body_hashtags(body_html or '').items():
        if name not in names and len(name) <= TAG_NAME_MAX:
            names.add(name)
            tags.append(find_hashtag_or_create(spelling))   # a new Tag shows the spelling first used
    return tags


_MEDIA_POST_TYPE = {MEDIA_KIND_VIDEO: POST_TYPE_VIDEO, MEDIA_KIND_AUDIO: POST_TYPE_LINK}   # A13.5


def media_upload_kind(user, community, type, uploaded_file) -> str | None:
    """The media kind `uploaded_file` is stored as, or None when it is not a media upload (A13.6): the post type must
    be the one `community`'s kind takes, and `user` a moderator the upload switch admits."""
    kind = media_kind_for(community)
    if kind is None or not uploaded_file or _MEDIA_POST_TYPE[kind] != type:
        return None
    return kind if can_upload_media(user, community) else None


def web_form_url(input, type):
    """The link a web form submitted for a post of `type`. The audio form names its link `audio_url`."""
    if type == POST_TYPE_LINK:
        field = input.audio_url if hasattr(input, 'audio_url') else input.link_url
    elif type == POST_TYPE_VIDEO:
        field = input.video_url
    else:
        return None
    return (field.data or '').strip()


def make_post(input, community, type, src, auth=None, uploaded_file=None, album=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
        if extra_rate_limit_check(user):
            raise Exception('rate_limited')
        title = input['title']
        url = input['url']
        language_id = input['language_id']
        # D1407. The web forms require `^https?://` of this field
        # (`CreateLinkForm.link_url`, `CreateVideoForm.video_url`); the API schema only
        # DOCUMENTS it -- `fields.String(metadata={"format": "url"})`, and marshmallow
        # does not validate `metadata`. The banned-domain check below is no help either:
        # `domain_from_url('javascript:alert(1)')` is None, so the whole block is skipped.
        # Ten templates render `post.url` as a bare href (D1404), so any authenticated
        # API client could post a clickable `javascript:` link. Measured:
        #
        #     PROBE api post.url = 'javascript:alert(document.domain)'
        #
        # Refused rather than dropped, as the API's avatar and cover are (D1405): the
        # caller is waiting for an answer and can fix the value.
        if url and _as_url(url) is None:
            raise Exception('url must be an http:// or https:// url')

    else:
        user = current_user
        title = input.title.data.strip()
        url = web_form_url(input, type)
        language_id = input.language_id.data

    # taking values from one JSON to put in another JSON to put in a DB to put in another JSON feels bad
    # instead, make_post shares code with edit_post
    # ideally, a similar change could be made for incoming activitypub (create_post() and update_post_from_activity() could share code)
    # once this happens, and post.new() just does the minimum before being passed off to an update function, post.new() can be used here again.

    if not can_create_post(user, community) or user_ip_banned():
        raise Exception('You are not permitted to make posts in this community')

    visibility = requested_visibility(input, src, community)

    if url:
        url = url.strip()
        domain = domain_from_url(url)
        if domain:
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')

    media_upload = media_upload_kind(user, community, type, uploaded_file) is not None
    if uploaded_file and uploaded_file.filename != '' and not media_upload:
        # check if this is an allowed type of file. A media upload is checked by its bytes instead (A13.6).
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.mpo', '.avif', '.svg']
        file_ext = os.path.splitext(uploaded_file.filename)[1]
        if file_ext.lower() not in allowed_extensions:
            raise Exception('filetype not allowed')

    post = Post(user_id=user.id, community_id=community.id, instance_id=user.instance_id, from_bot=user.bot or user.bot_override,
                posted_at=utcnow(), ap_id=gibberish(), title=title, language_id=language_id, visibility=visibility,
                local_only=content_local_only(user, community))
    if media_upload:   # A13.1: committed below, before the file is copied: never listed, not even for that while
        post.status = POST_STATUS_PROCESSING
    db.session.add(post)
    db.session.commit()

    post.up_votes = 1
    effect = 1.0
    post.score = post.up_votes * effect
    post.ranking = post.post_ranking(post.score, post.posted_at)
    post.ranking_scaled = int(post.ranking + community.scale_by())
    cache.delete_memoized(recently_upvoted_posts, user.id)

    community.post_count += 1
    community.last_active = g.site.last_active = utcnow()
    user.post_count += 1
    stamp_activity(user)
    apply_audit(user, 'ip_address', audit_ip(user, ip_address()))

    post.generate_ap_id(community)

    vote = PostVote(user_id=user.id, post_id=post.id, author_id=user.id, effect=1)
    db.session.add(vote)
    db.session.commit()

    try:    # federation is done in edit_post
        post = edit_post(input, post, type, src, user, auth, uploaded_file, from_scratch=True, album=album)
        published = post.status == POST_STATUS_PUBLISHED
        if post.status == POST_STATUS_PROCESSING:
            # Stored and committed: probe_media publishes it when the media is ready. Queued only once this create
            # has federated nothing and decided to notify nobody: an eager probe_media has published it by its return
            queue_probe(post.id)
    except Exception as e:
        # Only the file store_uploaded_media wrote during this call, never a path derived from post.url
        stored_media = getattr(post, 'stored_media_path', None)
        if stored_media is not None:
            with contextlib.suppress(FileNotFoundError):   # an eager probe_media may have moved it to S3 already
                os.unlink(stored_media)
        db.session.delete(vote)
        db.session.delete(post)
        community.post_count -= 1   # undo the increments above, or a post that never existed is counted (D463)
        user.post_count -= 1
        db.session.commit()
        raise e

    if published:
        notify_about_post(post)

    plugins.fire_hook('after_post_create', post)

    if src == SRC_API:
        return user.id, post
    else:
        return post


def set_local_album(post: Post, images: list) -> None:
    """Make `post.gallery` the pictures after the post's image (Pixelfed interop R5), replacing what it held: each
    `{url, alt_text}` and, for one of our uploads, the `file_name` and `hash` it was stored with, at weight 1.. in
    order. The layout of an inbound album (set_post_gallery), so rendering, the API and the serializers read it as
    they read one. A picture already in the album keeps its File, sized and with our upload's file left on disk; one
    that leaves the album is deleted with its file; a new one is sized. The caller commits."""
    rows = db.session.execute(post_file.select().where(post_file.c.post_id == post.id)).all()
    old = {file.source_url: file for file in File.query.filter(File.id.in_([row.file_id for row in rows])).all()}
    db.session.execute(post_file.delete().where(post_file.c.post_id == post.id))
    for weight, image in enumerate(images, start=1):
        file = old.pop(image['url'], None)
        if file is None:
            file = File(source_url=image['url'], file_name=image.get('file_name'), hash=image.get('hash'))
            db.session.add(file)
            db.session.flush()
            make_image_sizes(file.id, 512, 1200, 'posts', post.community.low_quality)
        file.alt_text = image['alt_text']
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
    for file in old.values():
        file.delete_from_disk()
        db.session.delete(file)
    post.gallery_count = len(images)


# 'from_scratch == True' means that it's not really a user edit, we're just re-using code for make_post()
def edit_post(input, post: Post, type, src, user=None, auth=None, uploaded_file=None, from_scratch=False, hash=None,
              album=None):
    if src == SRC_API and not user:   # authenticated first: the gone check must not answer an unauthorised caller
        user = authorise_api_user(auth, return_type='model', id_match=post.user_id)
    if not from_scratch and post.community.is_gone():   # interop C1 item 10: a Group deleted on its home server is read-only
        raise Exception('This community was deleted on its home server')
    if album is not None and len(album) + 1 > max_album_images():   # the image and its album (R7)
        raise Exception(f'At most {max_album_images()} images in one post')
    if album and any(_as_url(image['url']) is None for image in album):   # D1407, as for `url`: rendered as hrefs
        raise Exception('Each album url must be an http:// or https:// url')
    if src == SRC_API:
        title = input['title'].strip()
        body = input['body']
        url = input['url']
        # D1407. The web forms require `^https?://` of this field
        # (`CreateLinkForm.link_url`, `CreateVideoForm.video_url`); the API schema only
        # DOCUMENTS it -- `fields.String(metadata={"format": "url"})`, and marshmallow
        # does not validate `metadata`. The banned-domain check below is no help either:
        # `domain_from_url('javascript:alert(1)')` is None, so the whole block is skipped.
        # Ten templates render `post.url` as a bare href (D1404), so any authenticated
        # API client could post a clickable `javascript:` link. Measured:
        #
        #     PROBE api post.url = 'javascript:alert(document.domain)'
        #
        # Refused rather than dropped, as the API's avatar and cover are (D1405): the
        # caller is waiting for an answer and can fix the value.
        if url and _as_url(url) is None:
            raise Exception('url must be an http:// or https:// url')
        nsfw = input['nsfw']
        ai_generated = input['ai_generated']
        notify_author = input['notify_author']
        language_id = input['language_id']
        timezone = input['timezone'] if 'timezone' in input else user.timezone
        image_alt_text = input['image_alt_text'] if 'image_alt_text' in input else ''
        if image_alt_text is None:
            image_alt_text = ''
        if 'tags' in input:
            tags = tags_from_string_old(input['tags'])
        else:
            tags = []
        if 'flair' in input:
            flair = flairs_from_string(input['flair'], post.community_id)
        elif 'flair_id' in input and input['flair_id']:
            # Handle single flair_id for RSS feeds and other API calls
            flair_id = input['flair_id']
            if isinstance(flair_id, int):
                flair = [db.session.get(CommunityFlair, flair_id)]
            else:
                flair = CommunityFlair.query.filter(CommunityFlair.id.in_(flair_id)).all()
            flair = [f for f in flair if f is not None]
        else:
            flair = []
        scheduled_for = None
        repeat = None

        # Parse event data from API
        event_data = input.get('event', None)
        if event_data:
            # Parse datetime strings to datetime objects. Events are wall-clock times: the digits are kept
            # naive, read in the event's own timezone, and any offset is dropped. Polls, below, are UTC
            # instants instead. Intended, not an inconsistency (D297).
            if 'start' in event_data:
                if isinstance(event_data['start'], str):
                    event_data['start'] = datetime.fromisoformat(event_data['start'].replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    event_data['start'] = event_data['start']
            if 'end' in event_data and event_data['end']:
                if isinstance(event_data['end'], str):
                    event_data['end'] = datetime.fromisoformat(event_data['end'].replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    event_data['end'] = event_data['end']

        # Parse poll data from API
        poll_data = input.get('poll', None)
        if poll_data:
            # Extract all poll fields
            parsed_poll = {
                'mode': poll_data.get('mode', 'single'),
                'local_only': poll_data.get('local_only', False),
                'choices': poll_data.get('choices', [])
            }
            if 'end_poll' in poll_data and poll_data['end_poll']:
                # A UTC instant: the offset is honoured, unlike an event's wall-clock times above (D297)
                if isinstance(poll_data['end_poll'], str):
                    parsed_poll['end_poll'] = datetime.fromisoformat(poll_data['end_poll'].replace('Z', '+00:00'))
                else:
                    parsed_poll['end_poll'] = poll_data['end_poll']

            poll_data = parsed_poll
    else:
        if not user:
            user = current_user
        title = input.title.data.strip()
        body = input.body.data
        if type == POST_TYPE_IMAGE and not from_scratch:
            url = post.url
        else:
            url = web_form_url(input, type)
        nsfw = input.nsfw.data
        ai_generated = input.ai_generated.data
        notify_author = input.notify_author.data
        language_id = input.language_id.data
        tags = tags_from_string_old(input.tags.data)
        if input.flair:
            flair = flair_from_form(input.flair.data)
        else:
            flair = []
        scheduled_for = input.scheduled_for.data
        repeat = input.repeat.data
        timezone = input.timezone.data
        image_alt_text = input.image_alt_text.data if hasattr(input, 'image_alt_text') and input.image_alt_text else ''
        if getattr(input, 'episode_number', None) is not None and episode_form_wanted(post.community):
            # A17.4: the web form of a podcast episode; the API never has them. make_post comes through here too.
            # Not a guard on its own: any post in a local podcast community passes it, so a form that still has the
            # fields writes them. The routes prune the fields from forms of posts that are not episodes.
            with_episode(post, input.episode_number.data, input.season.data, input.explicit.data)

        # Extract poll data from form
        if type == POST_TYPE_POLL:
            poll_choices = []
            for i in range(1, 16):
                choice_text = getattr(input, f"choice_{i}").data
                if choice_text:
                    poll_choices.append({'choice_text': choice_text.strip(), 'sort_order': i})
            poll_data = {
                'mode': input.mode.data,
                'local_only': input.local_only.data,
                'choices': poll_choices
            }
            if input.finish_in:
                poll_data['end_poll'] = end_poll_date(input.finish_in.data)
        else:
            poll_data = None

        # Extract event data from form
        if type == POST_TYPE_EVENT:
            local_tz = ZoneInfo(input.event_timezone.data)
            local_start = input.start_datetime.data.replace(tzinfo=local_tz)
            local_end = input.end_datetime.data.replace(tzinfo=local_tz)
            utc_start = local_start.astimezone(ZoneInfo('UTC'))
            utc_end = local_end.astimezone(ZoneInfo('UTC'))

            event_data = {
                'start': utc_start.replace(tzinfo=None),
                'end': utc_end.replace(tzinfo=None),
                'timezone': input.event_timezone.data,
                'max_attendees': input.max_attendees.data,
                'online': input.online.data,
                'online_link': input.online_link.data,
                'more_info_url': input.more_info_url.data,  # R223
                'join_mode': input.join_mode.data,
                'location': {
                    'address': input.irl_address.data,
                    'city': input.irl_city.data,
                    'country': input.irl_country.data
                }
            }
        else:
            event_data = None

    # a file of ours an API client was shown on any storefront is the post's to federate, and the onion's host is
    # never stored; a link to a federating domain of ours keeps the host typed (plan C3, `stored_link`)
    url = stored_link(url, object_base(post))
    if is_upload_link(post.url) and link_on(post.url, object_base(post)) == url:
        url = post.url   # the stored file of ours, however it was spelt when stored: a stored URL is never rewritten

    # WARNING: beyond this point do not use the input variable as it can be either a dict or a form object!

    if media_of(post) is not None:   # A13.7: an edit cannot replace a media post's file, by a new file or a new url
        url = post.url
        uploaded_file = None
    media_kind = media_upload_kind(user, post.community, type, uploaded_file) if from_scratch else None
    if media_of(post) is not None or media_kind is not None:
        repeat = None   # a repeat copies the post, and every copy would share its one media file and thumbnail

    post.indexable = user.indexable
    if can_mod_post(post, user):
        post.sticky = False if src == SRC_API else input.sticky.data
    post.nsfw = nsfw or post.community.nsfw
    post.nsfl = False if src == SRC_API else input.nsfl.data
    post.ai_generated = ai_generated
    post.notify_author = notify_author
    post.language_id = language_id
    user.language_id = language_id
    post.title = title
    post.body = piefed_markdown_to_lemmy_markdown(body, object_base(post))   # the post's domain names it (C3)
    post.body_html = markdown_to_html(post.body, link_base=object_base(post))
    post.type = type
    post.scheduled_for = scheduled_for
    post.repeat = repeat
    post.timezone = timezone

    if post.url:
        if host_of(post.url) in {'pixelfed.social', 'pixelfed.uno'}:  # one pixelfed test, as below (D478)
            post.type = POST_TYPE_IMAGE
        elif post.url.startswith('https://loops.video/'):
            post.type = POST_TYPE_VIDEO
        elif is_video_url(post.url):
            post.type = POST_TYPE_VIDEO
        elif is_image_url(post.url):
            post.type = POST_TYPE_IMAGE

    if scheduled_for and post.status != POST_STATUS_PROCESSING:   # A13.7: processing media is published by probe_media
        date_with_tz = post.scheduled_for.replace(tzinfo=ZoneInfo(post.timezone))
        if date_with_tz.astimezone(ZoneInfo('UTC')) > utcnow(naive=False):
            post.status = POST_STATUS_SCHEDULED
    
    url_changed = False
    hash = None
    uploaded_name = None   # the name the upload below gives the file it writes (File.wrote_its_source)

    if not from_scratch:
        # Remove any subscription that currently exists
        existing_notification = NotificationSubscription.query.filter(NotificationSubscription.entity_id == post.id,
                                                                      NotificationSubscription.user_id == user.id,
                                                                      NotificationSubscription.type == NOTIF_POST).first()
        if existing_notification:
            db.session.delete(existing_notification)

        # Remove any poll votes that currently exists
        # Partially because it's easier to code but also to stop malicious alterations to polls after people have already voted
        db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'), {'post_id': post.id})
        db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

        # Remove any old images, and set url_changed
        if url != post.url or uploaded_file:
            url_changed = True
            if post.image_id:
                remove_file = db.session.get(File, post.image_id)
                if remove_file:
                    remove_file.delete_from_disk()
                post.image_id = None
            if post.url:
                domain = domain_from_url(post.url)
                if domain:
                    domain.post_count -= 1
                if post.type == POST_TYPE_VIDEO and store_files_in_s3() and post.url.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}'):
                    from app.shared.tasks.maintenance import delete_from_s3  # cycle: app.shared.tasks.maintenance imports delete_post from this module
                    if current_app.debug:
                        delete_from_s3([post.url])
                    else:
                        delete_from_s3.delay([post.url])

        # remove any old tags
        post.tags.clear()
        post.flair.clear()

        post.edited_at = utcnow()

        db.session.commit()

    if media_kind is not None:
        refusal = store_uploaded_media(post, uploaded_file, media_kind)
        if refusal is not None:
            raise MediaRefused(refusal)
        url = None   # the stored file is post.url; a link sent alongside it is not read
    elif uploaded_file and uploaded_file.filename != '':
        # check if this is an allowed type of file
        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.mpo', '.avif', '.svg']
        file_ext = os.path.splitext(uploaded_file.filename)[1]
        if file_ext.lower() not in allowed_extensions:
            raise Exception('filetype not allowed')

        new_filename = gibberish(15)
        # set up the storage directory
        if store_files_in_s3():
            directory = 'app/static/tmp'
        else:
            directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
        ensure_directory_exists(directory)

        # check for AI-generated images
        uploaded_file.seek(0)
        ai_gen = inspect_image_c2pa(uploaded_file.read(), uploaded_file.mimetype or "application/octet-stream")
        if ai_gen['c2pa']['ai_generated']:
            post.ai_generated = True

        # save the file
        final_place = os.path.join(directory, new_filename + file_ext.lower())
        uploaded_file.seek(0)
        uploaded_file.save(final_place)

        final_ext = file_ext.lower()  # track file extension for conversion

        if final_ext in ('.heic', '.heif'):
            register_heif_opener()
        if final_ext == '.avif':
            import pillow_avif  # NOQA  # do not remove  # lazy: registers Pillow's AVIF plugin only on the AVIF path
        if final_ext == '.svg':
            # An SVG that cannot be sanitized is rejected: the '.svg' branch
            # below skips the Pillow re-encode, so nothing downstream would
            # repair or re-check the file. sanitize_svg has already destroyed it
            # by the time it returns False.
            if not sanitize_svg(final_place):
                raise Exception('SVG file could not be sanitized')

        # Use environment variables to determine image max dimension, format, and quality
        image_max_dimension = current_app.config['MEDIA_IMAGE_MAX_DIMENSION']
        image_format = current_app.config['MEDIA_IMAGE_FORMAT']
        image_quality = current_app.config['MEDIA_IMAGE_QUALITY']

        if image_format == 'AVIF':
            import pillow_avif  # NOQA  # do not remove  # lazy: registers Pillow's AVIF plugin only on the AVIF path

        if not final_place.endswith('.svg') and not final_place.endswith('.gif') and not is_video_url(final_place):
            img = open_uploaded_image(final_place, allowed_extensions)
            img = ImageOps.exif_transpose(img)
            if (image_format == 'JPEG' or final_ext in ['.jpg', '.jpeg', '.mpo']):
                img = to_srgb(img)
            else:
                img = img.convert('RGBA')
            img.thumbnail((image_max_dimension, image_max_dimension), resample=Image.LANCZOS)

            kwargs = {}
            original_place = final_place
            if image_format:
                kwargs['format'] = image_format.upper()
                final_ext = '.' + image_format.lower()
                final_place = os.path.splitext(final_place)[0] + final_ext
            if image_quality:
                kwargs['quality'] = int(image_quality)

            save_image_without_metadata(img, final_place, optimize=True, **kwargs)
            if final_place != original_place:
                os.unlink(original_place)   # the upload was re-encoded under a new extension (D465)

        # D1363; on the post's domain, which federates it: a moderator's upload into someone else's post too (C3)
        url = f"{object_base(post)}{served_path(final_place)}"
        uploaded_name = os.path.basename(final_place)

        if current_app.config['IMAGE_HASHING_ENDPOINT'] and not is_video_url(final_place):
            hash = retrieve_image_hash(url)
            if hash and hash_matches_blocked_image(hash):
                raise Exception('This image is blocked')

        # Move uploaded file to S3
        if store_files_in_s3():
            session = boto3.session.Session()
            extra_args = {'ContentType': guess_mime_type(final_place)}
            if current_app.config.get('S3_STORAGE_CLASS'):
                extra_args['StorageClass'] = current_app.config['S3_STORAGE_CLASS']
            if current_app.config.get('S3_PUBLIC_ACL'):
                extra_args['ACL'] = 'public-read'
            s3 = session.client(
                service_name='s3',
                region_name=current_app.config['S3_REGION'],
                endpoint_url=current_app.config['S3_ENDPOINT'],
                aws_access_key_id=current_app.config['S3_ACCESS_KEY'],
                aws_secret_access_key=current_app.config['S3_ACCESS_SECRET'],
            )
            s3.upload_file(final_place, current_app.config['S3_BUCKET'], 'posts/' +
                           new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext,
                           ExtraArgs=extra_args)
            url = f"https://{current_app.config['S3_PUBLIC_URL']}/posts/" + \
                  new_filename[0:2] + '/' + new_filename[2:4] + '/' + new_filename + final_ext
            s3.close()
            os.unlink(final_place)

    if url and (from_scratch or url_changed):
        domain = domain_from_url(url)
        if domain:
            if domain.banned or domain.name.endswith('.pages.dev'):
                raise Exception(domain.name + ' is blocked by admin')
            post.domain = domain
            domain.post_count += 1
            already_notified = set()  # often admins and mods are the same people - avoid notifying them twice
            targets_data = {'gen': '0',
                            'post_id': post.id,
                            'orig_post_title': post.title,
                            'orig_post_body': post.body,
                            'orig_post_domain': post.domain.name,
                            # D1391. Renamed from `author_user_name`, which no
                            # consumer of THIS subtype read:
                            # app/templates/user/notifs/20.html:110 reads
                            # `suspect_user_user_name`, the same name the report
                            # templates use. `author_user_name` remains the right
                            # key in the dicts for other subtypes, whose own blocks
                            # do read it -- it was simply the wrong one here.
                            #
                            # Renamed rather than added, so all four writers of
                            # this dict now have the SAME shape, which is reason (2)
                            # of the arbitration recorded in
                            # tests/test_ap_update_post_tails.py's banner.
                            'suspect_user_id': user.id,
                            'suspect_user_user_name': user.stored_link(),
                            }
            if domain.notify_mods:
                for community_member in post.community.moderators():
                    if community_member.user.is_local():
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                              user_id=community_member.user_id, author_id=user.id,
                                              notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)
                        already_notified.add(community_member.user_id)
            if domain.notify_admins:
                for admin in Site.admins():
                    if admin.id not in already_notified:
                        notify = Notification(title='Suspicious content', url=post.ap_id,
                                              user_id=admin.id, author_id=user.id,
                                              notif_type=NOTIF_REPORT,
                                              subtype='post_from_suspicious_domain',
                                              targets=targets_data)
                        db.session.add(notify)

        thumbnail_url, embed_url = fixup_url(url)
        if is_image_url(url):
            file = File(source_url=url, hash=hash, file_name=uploaded_name)
            if (uploaded_file and type == POST_TYPE_IMAGE) or type == POST_TYPE_LINK:
                # change this line when uploaded_file is supported in API
                file.alt_text = image_alt_text
            db.session.add(file)
            db.session.commit()
            post.image_id = file.id

            
            # For events, uploaded images are banners - don't change post type or URL
            if type == POST_TYPE_EVENT:
                post.url = None  # Events don't have URLs when they have banner images
                make_image_sizes(post.image_id, 170, 2000, 'posts', post.community.low_quality)
            else:
                make_image_sizes(post.image_id, 512, 1200, 'posts', post.community.low_quality)
                post.type = POST_TYPE_IMAGE
                post.url = url
        elif host_of(url) in {'pixelfed.social', 'pixelfed.uno'}:  # one pixelfed test, as above (D478)
            post.type = POST_TYPE_IMAGE
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                    post.image = file
                    db.session.add(file)
            post.url = url
            post.body += '\n\nSource: '
        elif url.startswith('https://loops.video'):
            post.type = POST_TYPE_VIDEO
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    filename = filename.replace('.jpg', '.720p.mp4')
                    file = File(source_url=filename, alt_text=shorten_string(opengraph.get('og:title'), 295))
                    post.image = file
                    db.session.add(file)
            post.url = url
        else:
            opengraph = opengraph_parse(thumbnail_url)
            if opengraph and (opengraph.get('og:image', '') != '' or opengraph.get('og:image:url', '') != ''):
                filename = opengraph.get('og:image') or opengraph.get('og:image:url')
                if not filename.startswith('/'):
                    file = url_to_thumbnail_file(filename)
                    if file:
                        file.alt_text = shorten_string(opengraph.get('og:title'), 295)
                        post.image = file
                        db.session.add(file)

            post.url = embed_url

            if is_video_url(url) or url.endswith('.mp4') or url.endswith('.webm') or url.endswith('.mov') or \
                    is_video_hosting_site(embed_url):
                post.type = POST_TYPE_VIDEO
            else:
                post.type = POST_TYPE_LINK

        post.calculate_cross_posts(url_changed=url_changed)
    elif url and is_video_hosting_site(url):
        post.type = POST_TYPE_VIDEO
    
    if url and post.image:
        # D856: through the relationship. post.image_id is only synced at FLUSH, so a lookup by it found nothing
        # unless something upstream happened to emit SQL, and the caller's alt text was dropped for some urls.
        post.image.alt_text = image_alt_text

    # The album after the image (Pixelfed interop R5): None leaves it as it is, a list replaces it, and a post that is
    # no longer an image post has none
    if post.type != POST_TYPE_IMAGE or post.image is None:
        if album:
            raise Exception('An album needs an image post: give its first picture as the url')
        set_local_album(post, [])
    elif album is not None:
        set_local_album(post, album)

    federate = True
    if type == POST_TYPE_POLL and poll_data:
        post.type = POST_TYPE_POLL

        # Add poll choices
        if 'choices' in poll_data:  # pragma: no branch -- see proof in test_shared_post_url.py
            for choice in poll_data['choices']:
                if 'choice_text' in choice and choice['choice_text'].strip():
                    db.session.add(PollChoice(
                        post_id=post.id,
                        choice_text=choice['choice_text'].strip(),
                        sort_order=choice.get('sort_order', 1)
                    ))

        poll = Poll.query.filter_by(post_id=post.id).first()
        if poll is None:
            poll = Poll(post_id=post.id)
            db.session.add(poll)
        poll.mode = poll_data.get('mode', 'single')
        if 'end_poll' in poll_data and poll_data['end_poll']:
            poll.end_poll = poll_data['end_poll']
        poll.local_only = poll_data.get('local_only', False)
        poll.latest_vote = None
        db.session.commit()

        if poll.local_only:
            federate = False

    if type == POST_TYPE_EVENT and event_data:
        post.type = POST_TYPE_EVENT
        event = Event.query.filter_by(post_id=post.id).first()
        if event is None:
            event = Event(post_id=post.id)
            db.session.add(event)

        if 'start' in event_data:
            event.start = event_data['start']
        if 'end' in event_data and event_data['end']:
            event.end = event_data['end']

        event.timezone = event_data.get('timezone', 'UTC')
        event.max_attendees = event_data.get('max_attendees', 0)
        event.participant_count = event_data.get('participant_count', 0)
        event.full = event_data.get('full', False)
        event.online = event_data.get('online', False)
        # D1407. The same rule the federated path applies in `Post.new` and
        # `update_post_from_activity` (D1403), at the LOCAL producer of the same three
        # columns. This block serves both the web form and the API: the form validates
        # `online_link` with `Regexp(r'^https?://')` and does not offer the other two at
        # all, while the API accepts all three as `fields.String` with a documented
        # format and no validation. Measured, through `make_post(..., src=SRC_API)`:
        #
        #     PROBE api online_link                = 'javascript:alert(document.domain)'
        #     PROBE api external_participation_url = 'javascript:alert(2)'
        #     PROBE api buy_tickets_link           = 'javascript:alert(3)'
        #
        # `post/_post_full.html:219` renders `online_link` as a bare href. Dropped rather
        # than refused, matching `_as_url`'s use on the federated side: None is what these
        # columns hold for an event that named no link, and the rest of the event is still
        # what the author asked for.
        event.online_link = _as_url(event_data.get('online_link'), 1024)
        event.more_info_url = _as_url(event_data.get('more_info_url'), 1024)  # R223
        event.join_mode = event_data.get('join_mode', 'free')
        event.external_participation_url = _as_url(
            event_data.get('external_participation_url'), 1024)
        event.anonymous_participation = event_data.get('anonymous_participation', False)
        event.buy_tickets_link = _as_url(event_data.get('buy_tickets_link'), 1024)
        event.event_fee_currency = event_data.get('event_fee_currency')
        event.event_fee_amount = event_data.get('event_fee_amount', 0)
        if 'location' in event_data:
            event.location = event_data['location']
        db.session.commit()

    # add tags & flair
    if is_local_microblogs(post.community):
        tags = with_body_hashtags(tags, post.body_html)
    post.tags = tags
    post.flair = flair

    # Add subscription if necessary
    if notify_author:
        new_notification = NotificationSubscription(name=post.title, user_id=user.id, entity_id=post.id,
                                                    type=NOTIF_POST)
        db.session.add(new_notification)

    db.session.commit()

    # Scheduled and processing posts alike (A13.1): nothing federates before the post is published
    if post.status < POST_STATUS_PUBLISHED or post.community.local_only or post.community.private:
        federate = False

    if from_scratch:
        if federate:
            task_selector('make_post', post_id=post.id)
    elif federate:
        task_selector('edit_post', post_id=post.id)

    if src == SRC_API:
        if from_scratch:
            return post
        else:
            return user.id, post
    elif from_scratch:
        return post


# just for deletes by owner (mod deletes are classed as 'remove')
def delete_post(post_id: int, federate_deletion, src, auth):
    if src == SRC_API:
        post = db.session.get(Post, post_id) or abort(404)
        user_id = authorise_api_user(auth, id_match=post.user_id)
    else:
        if current_user:
            user_id = current_user.id
        else:
            user_id = 1     # for remove_old_community_content()

    with app_pkg.redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.get(Post, post_id) or abort(404)
        if post.deleted:  # already deleted: nothing to undo or federate
            return (user_id, post) if src == SRC_API else None
        if post.url:
            post.calculate_cross_posts(delete_only=True)

        post.deleted = True
        post.deleted_by = user_id
        post.author.post_count -= 1
        stamp_activity(post.author)
        post.community.post_count -= 1
        adjust_domain_post_count(post, -1)  # D1362
        db.session.commit()

    if federate_deletion and post.status == POST_STATUS_PUBLISHED:
        task_selector('delete_post', user_id=user_id, post_id=post.id)

    # remove any notifications about the post
    notifs = db.session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == post.id)
    for notif in notifs:
        # dont delete report notifs
        if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
            continue
        db.session.delete(notif)
    db.session.commit()

    if src == SRC_API:
        return user_id, post
    else:
        return


def restore_post(post_id: int, src, auth):
    if src == SRC_API:
        post = db.session.get(Post, post_id) or abort(404)
        user_id = authorise_api_user(auth, id_match=post.user_id)
    else:
        user_id = current_user.id

    with app_pkg.redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.get(Post, post_id) or abort(404)
        if not post.deleted:  # not deleted: nothing to restore or federate
            return (user_id, post) if src == SRC_API else None
        if post.deleted_by != post.user_id:  # D421: an author may not undo a moderator's removal
            if src == SRC_API:
                raise Exception('Does not have permission')
            abort(403)
        if post.url:
            post.calculate_cross_posts()

        post.deleted = False
        post.deleted_by = None
        post.author.post_count += 1
        stamp_activity(post.author)
        post.community.post_count += 1
        adjust_domain_post_count(post, 1)  # D1362
        db.session.commit()

    if post.status == POST_STATUS_PUBLISHED:
        task_selector('restore_post', user_id=user_id, post_id=post.id)

    if src == SRC_API:
        return user_id, post
    else:
        return


def report_post(post: Post, input, src, auth=None):
    if src == SRC_API:
        reporter_user = authorise_api_user(auth, return_type='model')
        suspect_user = User.query.filter_by(id=post.user_id).one()
        reason = input['reason']
        description = input['description']
        notify_admins = (any(x in reason.lower() for x in ['minor abuse', 'doxing']) or
                        any(x in description.lower() for x in ['minor abuse', 'doxing']) or
                         (reason == 'AI content that needs flair' and post.community.instance.software.lower() != 'piefed'))
        report_remote = input['report_remote']
    else:
        reporter_user = current_user
        suspect_user = db.session.get(User, post.user_id)
        reason = input.reasons_to_string(input.reasons.data)
        description = input.description.data
        notify_admins = ('5' in input.reasons.data or '6' in input.reasons.data or ('17' in input.reasons.data and post.community.instance.software.lower() != 'piefed'))
        report_remote = input.report_remote.data

    # both arms name the author's instance, which a moved post's own instance_id no longer is
    source_instance = db.session.get(Instance, suspect_user.instance_id) if suspect_user.instance_id else None

    if post.community.is_local() and post.community.un_moderated:
        notify_admins = True

    targets_data = {
        'gen': '0',
        'suspect_post_id': post.id,
        'suspect_user_id': post.user_id,
        'suspect_user_user_name': suspect_user.stored_link(),
        'source_instance_id': source_instance.id if source_instance else None,
        'source_instance_domain': source_instance.domain if source_instance else None,
        'reporter_id': reporter_user.id,
        'reporter_user_name': reporter_user.stored_link(),
        'orig_post_title': post.title,
        'orig_post_body': post.body
    }
    # report.type 1 = 'post'
    report = Report(reasons=reason[:255],
                    description=description[:255],
                    type=REPORT_TYPE_POST,
                    reporter_id=reporter_user.id,
                    suspect_post_id=post.id,
                    suspect_community_id=post.community_id,
                    suspect_user_id=post.user_id,
                    in_community_id=post.community_id,
                    source_instance_id=reporter_user.instance_id,
                    targets=targets_data)
    db.session.add(report)

    # Notify local moderators, and send Flag to remote moderators
    # if user has not selected 'report_remote', just send to remote mods not on community's or suspect_users's instances
    already_notified = set()
    remote_instance_ids = set()
    for mod in post.community.moderators():
        moderator = db.session.get(User, mod.user_id)
        if moderator.is_local():
            with force_locale(get_recipient_language(moderator.id)):
                notification = Notification(user_id=mod.user_id, title=gettext('A post has been reported'),
                                            url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                            author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                            subtype='post_reported',
                                            targets=targets_data)
                db.session.add(notification)
                moderator.unread_notifications += 1
                already_notified.add(mod.user_id)
        else:
            if not report_remote:
                if moderator.instance_id != suspect_user.instance_id and moderator.instance_id != post.community.instance_id:
                    remote_instance_ids.add(moderator.instance_id)
            else:
                remote_instance_ids.add(moderator.instance_id)

    if notify_admins:
        for admin in Site.admins():
            if admin.id not in already_notified:
                notify = Notification(title='Suspicious content', url='/admin/reports', user_id=admin.id,
                                      author_id=reporter_user.id, notif_type=NOTIF_REPORT,
                                      subtype='post_reported',
                                      targets=targets_data)
                db.session.add(notify)
                admin.unread_notifications += 1

    # Lemmy doesn't process or generate Announce / Flag, so Flags also have to be sent from here to user's and community's instances
    if report_remote:
        if not post.community.is_local():
            if post.community.instance_id not in remote_instance_ids: # very unlikely, since it will typically have mods on same instance.
                remote_instance_ids.add(post.community.instance_id)
        if not suspect_user.is_local():
            if suspect_user.instance_id not in remote_instance_ids:
                remote_instance_ids.add(suspect_user.instance_id)

    post.reports += 1
    db.session.commit()

    if remote_instance_ids:
        summary = reason
        if description:
            summary += ' - ' + description

        task_selector('report_post', user_id=reporter_user.id, post_id=post.id, summary=summary, instance_ids=list(remote_instance_ids))

    if src == SRC_API:
        return reporter_user.id, report
    else:
        return


def lock_post(post_id: int, locked, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.get(Post, post_id) or abort(404)
    if locked:
        comments_enabled = False
        modlog_type = 'lock_post'
    else:
        comments_enabled = True
        modlog_type = 'unlock_post'

    if can_mod_post(post, user):
        post.comments_enabled = comments_enabled
        db.session.commit()
        add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                      community=post.community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

        if locked:
            if src == SRC_WEB:
                flash(_('%(name)s has been locked.', name=post_title_for(post, user.id)))
            task_selector('lock_post', user_id=user.id, post_id=post_id)
        else:
            if src == SRC_WEB:
                flash(_('%(name)s has been unlocked.', name=post_title_for(post, user.id)))
            task_selector('unlock_post', user_id=user.id, post_id=post_id)
    elif src == SRC_API:
        raise Exception('Does not have permission')

    if src == SRC_API:
        return user.id, post


def move_post(post_id: int, target_id: int, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.get(Post, post_id) or abort(404)

    if not is_open(post):
        # Amendment B (S7.4): a followers-only post lives only in the local microblogs community and was never
        # sent to any community's followers -- moving it would publish it to a new audience
        msg = 'A followers-only post cannot be moved.'
        if src == SRC_API:
            raise Exception(msg)
        flash(_(msg), 'error')
        return

    if can_mod_post(post, user):
        old_community_id = post.community_id
        target_community = db.session.get(Community, target_id)

        # D1102. Only the SOURCE community was ever consulted. A moderator of
        # any community could therefore move a post into any other one --
        # including a PRIVATE community they do not belong to, which is
        # content injected past its membership. Measured: `PROBE am1 mod moves
        # into a private community: 302 | post now in theirs (private=True)`.
        #
        # The test is the destination's MEMBERSHIP, not `can_create_post`:
        # moving is a moderation action, and `can_create_post` would also
        # bring in the poster-side conditions -- verification, keys,
        # `ban_posts` -- which are about authoring a new post and not about
        # where an existing one may be filed. Placed in this helper so the web
        # route and the API are covered by one check (fact 478).
        if target_community is None or target_community.banned or target_community.is_gone() or \
                (target_community.private and
                 target_community.id not in community_membership_private(user.id)) or \
                target_community.id in communities_banned_from(user.id):
            msg = 'You cannot post in that community.'
            if src == SRC_API:
                raise Exception(msg)
            flash(_(msg), 'error')
            return

        post.move_to(target_community)
        db.session.commit()

        add_to_modlog('move_post', actor=user, target_user=post.author, reason='',
                      community=target_community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

        if src == SRC_WEB:
            flash(_('%(name)s has been moved.', name=post.title))

        task_selector('move_post', user_id=user.id, old_community_id=old_community_id,
                      new_community_id=target_community.id, post_id=post_id)
    elif src == SRC_API:
        raise Exception('Does not have permission')

    if src == SRC_API:
        return user.id, post


def sticky_post(post_id: int, featured: bool, src: int, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.get(Post, post_id) or abort(404)

    if can_mod_post(post, user):
        _set_featured(post, user, featured)
    elif src == SRC_API:
        raise Exception('Does not have permission')

    return user.id, post


def _set_featured(post, user, featured: bool) -> None:
    community = post.community
    post.sticky = featured
    if featured:
        modlog_type = 'featured_post'
    else:
        modlog_type = 'unfeatured_post'
    if not community.ap_featured_url:
        community.ap_featured_url = community.ap_profile_id + '/featured'
    db.session.commit()
    add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    if featured:
        task_selector('sticky_post', user_id=user.id, post_id=post.id)
    else:
        task_selector('unsticky_post', user_id=user.id, post_id=post.id)


def feature_published_post(post) -> None:
    """Feature a post its author marked sticky when making it, once it is published. add_post leaves an unpublished
    post's sticky to here: an Add to the featured collection, and a modlog row, for a post no one can see yet would
    name an object peers cannot fetch."""
    if post.sticky and can_mod_post(post, post.author):
        _set_featured(post, post.author, True)


def hide_post(post_id: int, hidden: bool, src: int, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    post = db.session.get(Post, post_id) or abort(404)

    if hidden:
        user.mark_post_as_hidden(post)
    else:
        db.session.execute(text('DELETE FROM "hidden_posts" WHERE user_id = :user_id AND hidden_post_id = :post_id'),
                           {'user_id': user.id, 'post_id': post.id})
    db.session.commit()

    return user.id, post


def can_mod_post(post, user) -> bool:
    # D421: the one gate for moderating someone else's post, asked by the post routes and below
    return can_moderate(post.community, user)


# mod deletes
def mod_remove_post(post_id: int, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    with app_pkg.redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.get(Post, post_id) or abort(404)

        if not can_mod_post(post, user):
            raise Exception('Does not have permission')

        if post.url:
            post.calculate_cross_posts(delete_only=True)

        post.deleted = True
        post.deleted_by = user.id
        post.author.post_count -= 1
        post.community.post_count -= 1
        adjust_domain_post_count(post, -1)  # D1362
        db.session.commit()

    add_to_modlog('delete_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('delete_post', user_id=user.id, post_id=post.id, reason=reason)

    # remove any notifications about the post
    notifs = db.session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == post.id)
    for notif in notifs:
        # dont delete report notifs
        if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
            continue
        db.session.delete(notif)
    db.session.commit()

    if src == SRC_API:
        return user.id, post
    else:
        return


def mod_restore_post(post_id: int, reason, src, auth):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    with app_pkg.redis_client.lock(f"lock:post:{post_id}", timeout=10, blocking_timeout=6):
        post = db.session.get(Post, post_id) or abort(404)
        if not can_mod_post(post, user):
            raise Exception('Does not have permission')

        if post.url:
            post.calculate_cross_posts()

        post.deleted = False
        post.deleted_by = None
        post.author.post_count += 1
        post.community.post_count += 1
        adjust_domain_post_count(post, 1)  # D1362
        db.session.commit()

    add_to_modlog('restore_post', actor=user, target_user=post.author, reason=reason,
                  community=post.community, post=post,
                  link_text=shorten_string(post.title), link=f'post/{post.id}')

    task_selector('restore_post', user_id=user.id, post_id=post.id, reason=reason)

    if src == SRC_API:
        return user.id, post
    else:
        return


def mark_post_read(post_ids: List[int], read: bool, user_id: int):
    if read is True:
        for post_id in post_ids:
            db.session.execute(text(
                'INSERT INTO "read_posts" (user_id, read_post_id, interacted_at) VALUES (:user_id, :post_id, :stamp) ON CONFLICT (user_id, read_post_id) DO UPDATE SET interacted_at = EXCLUDED.interacted_at'),
                {"user_id": user_id, "post_id": post_id, "stamp": utcnow()})
    else:
        for post_id in post_ids:
            db.session.execute(
                text('DELETE FROM "read_posts" WHERE user_id = :user_id AND read_post_id = :post_id'),
                {"user_id": user_id, "post_id": post_id})
    with app_pkg.redis_client.lock(f"lock:user:{user_id}", timeout=10, blocking_timeout=6):
        if not is_onion_request():
            db.session.execute(text('UPDATE "user" SET last_seen = now() WHERE id = :user_id'),
                               {'user_id': user_id})
        db.session.commit()


def get_post_flair_list(post: Post | int) -> list:
    if isinstance(post, int):
        post = db.session.query(Post).filter_by(id=post).one()
    
    if not post.flair:
        # In case flair is null
        flair_list = []
    else:
        flair_list = post.flair
    
    return flair_list


def _poll_choice_id(choice_id):
    """A submitted poll choice id as an int, or None when it is not one (D415)."""
    try:
        return int(choice_id)
    except (TypeError, ValueError):
        return None


def vote_for_poll(post_id, votes, src, auth=None):
    if src == SRC_API:
        user = authorise_api_user(auth, return_type='model')
    else:
        user = current_user

    if user.banned or user_ip_banned():
        abort(403)
    
    if isinstance(votes, int):
        votes = [votes]

    poll = db.session.get(Poll, post_id) or abort(404)
    # PERM-2: voting in a poll is allowed exactly when replying to its post is
    if not can_create_post_reply(user, db.session.get(Post, post_id).community):
        msg = 'You are not allowed to vote in this poll.'
        if src == SRC_API:
            raise Exception(msg)
        flash(_(msg), 'error')
        return False
    poll_choice_ids = {row.id for row in
                       db.session.query(PollChoice).filter_by(post_id=post_id)}
    foreign = [choice_id for choice_id in votes if _poll_choice_id(choice_id) not in poll_choice_ids]
    if foreign:
        msg = 'Choice does not belong to this poll.'
        if src == SRC_API:
            raise Exception(msg)
        # D415: a single-mode vote has exactly one choice, so a foreign or
        # non-integer one leaves nothing to record -- refuse it, and tell the
        # route not to report the vote as cast.
        if poll.mode == 'single':
            flash(_(msg), 'error')
            return False
        votes = [choice_id for choice_id in votes if _poll_choice_id(choice_id) in poll_choice_ids]
        if not votes:
            return
    if poll.mode == 'single':
        if len(votes) != 1:
            if src == SRC_API:
                raise Exception("Poll is in single vote mode, only a single choice is allowed.")
        if not poll.has_voted(user.id):
            choice_id = int(votes[0])
            poll.vote_for_choice(choice_id, user.id)
            task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                        choice_text=db.session.get(PollChoice, choice_id).choice_text)
        else:
            if src == SRC_API:
                raise Exception("User has already voted.")
    else:
        for choice_id in votes:
            already_voted = db.session.query(PollChoiceVote).filter(
                PollChoiceVote.user_id == user.id,
                PollChoiceVote.choice_id == int(choice_id)).first() is not None
            poll.vote_for_choice(int(choice_id), user.id)
            if not already_voted:
                task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                              choice_text=db.session.get(PollChoice, int(choice_id)).choice_text)
