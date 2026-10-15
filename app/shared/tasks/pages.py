import json
from zoneinfo import ZoneInfo
from app import celery, db
from app.activitypub.serializers import microblog_note, uses_microblog_note
from app.activitypub.signature import default_context, send_post_request
from app.constants import POST_TYPE_LINK, POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_VIDEO, \
    POST_TYPE_POLL, MICROBLOG_APPS, NOTIF_MENTION, POST_TYPE_EVENT
from app.models import Instance, Notification, Poll, PollChoice, Post, User, UserFollower, utcnow, Event, \
    Community, more_info_link
from app.mentions import mentioned_users_found_by
from app.user.utils import search_for_user
from app.visibility import can_view, follower_instances, followers_only_addressing, is_open
from app.utils import user_banned_from_community, gibberish, instance_banned, ap_datetime, get_recipient_language, get_task_session, \
    patch_db_session, TaskError

from flask import current_app
from flask_babel import _, force_locale, gettext



""" Post JSON format
{
  'id':
  'type':
  'attributedTo':
  'to': []
  'cc': []
  'tag': []
  'audience':
  'content':
  'mediaType':
  'source': {}
  'inReplyTo':      (only added for Mentions and user followers, as Note with inReplyTo: None)
  'published':
  'updated':        (inner oject of Update only)
  'language': {}
  'name':           (not included in Polls, which are sent out as microblogs)
  'attachment': []
  'commentsEnabled':
  'sensitive':
  'nsfl':
  'stickied':
  'image':          (for posts with thumbnails only)
  'endTime':        (last 3 are for polls)
  'votersCount':
  'oneOf' / 'anyOf':
}
"""
""" Create / Update / Announce JSON format
{
  'id':
  'type':
  'actor':
  'object':
  'to': []
  'cc': []
  '@context':       (outer object only)
  'audience':       (not in Announce)
}
"""



@celery.task
def make_post(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_post(post_id, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def edit_post(send_async, post_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_post(post_id, edit=True, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def can_deliver_to(user, instance) -> bool:
    """True when `user`'s activity may go to `instance`'s shared inbox: it has one, is up, and neither the user has
    blocked it nor this instance banned it."""
    return bool(instance.inbox and instance.online() and not user.has_blocked_instance(instance.id)
                and not instance_banned(instance.domain))


def mentioned_users(post, allow_fetch=True) -> list:
    """The users a post's body Mentions with @user@instance syntax, once each, leaving out its author."""
    return mentioned_users_found_by(
        post, lambda address: search_for_user(address, allow_fetch=allow_fetch, retry=True))


def reply_recipients(reply, parent, allow_fetch=True) -> list:
    """Who a reply answers or names: `parent`'s author, then the users `reply` Mentions, once each."""
    recipients = [parent.author]
    recipients.extend(recipient for recipient in mentioned_users(reply, allow_fetch=allow_fetch)
                      if recipient.id != parent.author.id)
    return recipients


def send_microblog_note(post, user, community, recipients: list, edit: bool) -> None:
    """Push a local microblogs post as the Note microblog_note builds (spec 2026-10-14 §4.2, §6.1, §10 A6).

    Two independent fan-outs. The community Announces a public post to its threadiverse followers, as any local
    community does. The author's Create (or Update) goes to every mentioned user's instance and every follower's
    instance, whether or not that instance also got the Announce. A Mastodon follower given only a boost would not
    see the post as its author's own status, and a receiver dedupes by object id."""
    note = microblog_note(post, recipients)
    verb = 'update' if edit else 'create'
    activity = {'id': f"{current_app.config['SERVER_URL']}/activities/{verb}/{gibberish(15)}",
                'type': 'Update' if edit else 'Create', 'actor': user.public_url(), 'object': note,
                'to': note['to'], 'cc': note['cc'], '@context': default_context()}
    followers_only = not is_open(post)

    sent_to = set()
    if not followers_only:
        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        inner = {key: value for key, value in activity.items() if key != '@context'}
        microblog_announce = {'id': announce_id, 'type': 'Announce', 'actor': community.public_url(),
                              'object': post.ap_id, 'to': note['to'], 'cc': [community.ap_followers_url],
                              '@context': default_context()}
        # Lemmy and mbin file a post by its audience: only the copy for them names the community (spec §10 A9)
        audience = community.public_url()
        group_announce = dict(microblog_announce,
                              object=dict(inner, audience=audience, object=dict(note, audience=audience)))
        for instance in community.following_instances():
            if not can_deliver_to(user, instance):
                continue
            if instance.software in MICROBLOG_APPS:
                if edit:
                    send_post_request(instance.inbox, activity, user.private_key, user.public_url() + '#main-key')
                    sent_to.add(instance.domain)
                else:
                    send_post_request(instance.inbox, microblog_announce, community.private_key,
                                      community.public_url() + '#main-key')
            else:
                send_post_request(instance.inbox, group_announce, community.private_key,
                                  community.public_url() + '#main-key')

    targets = [recipient.instance for recipient in recipients if recipient.instance_id != 1]
    targets += follower_instances(user) if followers_only else user.following_instances()
    for instance in targets:
        if instance.domain in sent_to or not can_deliver_to(user, instance):
            continue
        send_post_request(instance.inbox, activity, user.private_key, user.public_url() + '#main-key')
        sent_to.add(instance.domain)


def send_post(post_id, edit=False, session=None):
    post = session.get(Post, post_id)
    user = post.author
    community = post.community

    post_body_html = post.body_html if post.body_html else ''

    # Find any users Mentioned in post body with @user@instance syntax
    recipients = mentioned_users(post)

    # Notify any local users that have been Mentioned
    for recipient in recipients:
        if recipient.is_local() and can_view(post, recipient.id):
            if edit:
                existing_notification = session.query(Notification).filter(Notification.user_id == recipient.id, Notification.url == f"{current_app.config['SERVER_URL']}/post/{post.id}").first()
            else:
                existing_notification = None
            if not existing_notification:
                targets_data = {'gen':'0',
                                'post_id':post.id,
                                'post_body':post.body,
                                'post_title': post.title,
                                'author_user_name': user.ap_id if user.ap_id else user.user_name
                                }
                with force_locale(get_recipient_language(recipient.id)):
                    notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in post {post.id}"),
                                                url=f"{current_app.config['SERVER_URL']}/post/{post.id}",
                                                author_id=user.id, notif_type=NOTIF_MENTION,
                                                subtype='post_mention',
                                                targets=targets_data)
                    recipient.unread_notifications += 1
                    session.add(notification)
                    session.commit()

    if not (community.instance_id and community.instance.online()):
        return

    # local_only communities do not federate
    if community.local_only or community.private or community.is_gone():   # interop C1 item 10: nothing goes to a gone Group
        return

    banned = user_banned_from_community(user.id, community.id)  # D995
    if banned:
        return
    if not community.is_local():
        if user.has_blocked_instance(community.instance.id) or instance_banned(community.instance.domain):
            return

    if uses_microblog_note(post):   # spec 2026-10-14 §10 A1: the Note push and fetch share one builder
        send_microblog_note(post, user, community, recipients, edit)
        return

    if post.type == POST_TYPE_POLL:
        type = 'Question'
    elif post.type == POST_TYPE_EVENT:
        type = 'Event'
    else:
        type = 'Page'
    # Amendment B (S7.2): a followers-only post is addressed to the author's followers, never to Public or the community
    followers_only = not is_open(post)
    if followers_only:
        addressing = followers_only_addressing(user, recipients)
    else:
        addressing = {'to': [community.public_url(), "https://www.w3.org/ns/activitystreams#Public"],
                      'cc': [recipient.public_url() for recipient in recipients],
                      'interactionPolicy': {'canQuote': {
                          'automaticApproval': ['https://www.w3.org/ns/activitystreams#Public']}}}
    to, cc = addressing['to'], addressing['cc']
    tag = post.tags_for_activitypub()
    for recipient in recipients:
        tag.append({'href': recipient.public_url(), 'name': recipient.mention_tag(), 'type': 'Mention'})
    language = {'identifier': post.language_code(), 'name': post.language_name()}
    source = {'content': post.body, 'mediaType': 'text/markdown'}
    attachment = []
    # EVENT too, as post_to_page attaches it, so the pushed document matches the fetched one (D300)
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        attachment.append({'href': post.url, 'type': 'Link'})
    elif post.type == POST_TYPE_IMAGE and post.image_id:
        attachment.append({'type': 'Image', 'url': post.image.source_url, 'name': post.image.alt_text})

    page = {
      'id': post.public_url(),
      'type': type,
      'attributedTo': user.public_url(),
      'to': to,
      'cc': cc,
      'tag': tag,
      'audience': community.public_url(),
      'content': post_body_html if post.type != POST_TYPE_POLL else '<p>' + post.title + '</p>' + post_body_html,
      'mediaType': 'text/html',
      'source': source,
      'published': ap_datetime(post.posted_at),
      'language': language,
      'name': post.title,
      'attachment': attachment,
      'commentsEnabled': post.comments_enabled,
      'sensitive': post.nsfw or post.nsfl,
      'nsfl': post.nsfl,
      'genAI': post.ai_generated,
      'stickied': post.sticky,
      'interactionPolicy': addressing['interactionPolicy'],
    }
    if post.type != POST_TYPE_POLL:
        page['name'] = post.title
    if edit:
        page['updated'] = ap_datetime(utcnow())
    if post.image_id:
        image_url = ''
        if post.image.source_url:
            image_url = post.image.source_url
        elif post.image.file_path:
            image_url = post.image.file_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
        elif post.image.thumbnail_path:
            image_url = post.image.thumbnail_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
        page['image'] = {'type': 'Image', 'url': image_url}
    # a post's type does not guarantee its Poll or Event row exists
    if post.type == POST_TYPE_POLL and (poll := Poll.query.filter_by(post_id=post.id).first()) is not None:
        if poll.end_poll is not None:
            page['endTime'] = ap_datetime(poll.end_poll)
        page['votersCount'] = poll.total_votes() if edit else 0
        choices = []
        for choice in PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all():
            choices.append({'type': 'Note', 'name': choice.choice_text, 'replies': {'type': 'Collection', 'totalItems': choice.num_votes if edit else 0}})
        page['oneOf' if poll.mode == 'single' else 'anyOf'] = choices
    elif post.type == POST_TYPE_EVENT and (event := Event.query.filter_by(post_id=post.id).first()) is not None:
        if event.start is not None:
            page['startTime'] = ap_datetime(event.start)
        if event.end is not None:
            page['endTime'] = ap_datetime(event.end)
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
        page.update({key: value for key, value in optional.items() if value is not None})
        if event.more_info_url:  # R223
            page['attachment'].append(more_info_link(event.more_info_url))

    activity = 'create' if not edit else 'update'
    create_id = f"{current_app.config['SERVER_URL']}/activities/{activity}/{gibberish(15)}"
    type = 'Create' if not edit else 'Update'
    create = {
      'id': create_id,
      'type': type,
      'actor': user.public_url(),
      'object': page,
      'to': to,
      'cc': cc,
      '@context': default_context(),
      'audience': community.public_url()
    }
    if followers_only:      # no community audience: Lemmy-likes would file it as a community post
        del page['audience']
        del create['audience']

    domains_sent_to = [current_app.config['SERVER_NAME']]

    # if the community is local, and remote instance is something like Lemmy, Announce the activity
    # if the community is local, and remote instance is something like Mastodon, Announce creates (so the community Boosts it), but send updates directly and from the user
    # Announce of Poll doesn't work for Mastodon, so don't add domain to domains_sent_to, so they receive it if they're also following the User or they get Mentioned
    # if the community is remote, send activity directly
    if followers_only:
        pass    # S7.2: it never reaches the community's followers, so the community never Announces it
    elif not community.local_only:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
        if community.is_local():
            del create['@context']

            announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
            cc = [community.ap_followers_url]
            group_announce = {
              'id': announce_id,
              'type': 'Announce',
              'actor': community.public_url(),
              'object': create,
              'to': to,
              'cc': cc,
              '@context': default_context()
            }
            microblog_announce = {
              'id': announce_id,
              'type': 'Announce',
              'actor': community.public_url(),
              'object': post.ap_id,
              'to': to,
              'cc': cc,
              '@context': default_context()
            }
            for instance in set(community.following_instances() + user.following_instances(software=['piefed', 'mbin', 'pylova'])):
                if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                    if instance.software in MICROBLOG_APPS:
                        if activity == 'create':
                            send_post_request(instance.inbox, microblog_announce, community.private_key, community.public_url() + '#main-key')
                        else:
                            send_post_request(instance.inbox, create, user.private_key, user.public_url() + '#main-key')
                    else:
                        send_post_request(instance.inbox, group_announce, community.private_key, community.public_url() + '#main-key')
                    if post.type != POST_TYPE_POLL:
                          domains_sent_to.append(instance.domain)
        else:
            send_post_request(community.ap_inbox_url, create, user.private_key, user.public_url() + '#main-key')
            domains_sent_to.append(community.instance.domain)

    # amend copy of the Create, for anyone Mentioned in post body or who is following the user, to a format more likely to be understood by Mastodon
    if '@context' not in create:
        create['@context'] = default_context()
    if 'name' in page:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
        del page['name']
    note = page
    note['content'] = ''
    if note['type'] == 'Page' or note['type'] == 'Event':
        note['type'] = 'Note'
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO) and post.url:
        note['content'] += '<p><a href=' + post.url + '>' + post.title + '</a></p>'
    elif post.type != POST_TYPE_POLL:
        note['content'] = '<p>' + post.title + '</p>'
    if post.type == POST_TYPE_EVENT and post.event is not None and post.event.start is not None:
        # Convert UTC time to event timezone
        # A peer can store a null timezone; read it as UTC, edit_post's default
        timezone = post.event.timezone or 'UTC'
        event_tz = ZoneInfo(timezone)
        local_start = post.event.start.replace(tzinfo=ZoneInfo('UTC')).astimezone(event_tz)
        note['content'] += '<p>' + local_start.strftime('%Y-%m-%dT%H:%M:%S') + f' ({timezone})</p>'
    if post_body_html:
        note['content'] = note['content'] + post_body_html
    if post.language_id:
        note['contentMap'] = {post.language_code(): note['content']}
    note['inReplyTo'] = None
    create['object'] = note
    if not community.local_only:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
        for recipient in recipients:
            if recipient.instance.domain not in domains_sent_to:
                if followers_only and not can_deliver_to(user, recipient.instance):
                    continue
                send_post_request(recipient.instance.inbox, create, user.private_key, user.public_url() + '#main-key')
                domains_sent_to.append(recipient.instance.domain)

    follower_query = session.query(UserFollower).filter_by(local_user_id=post.user_id, is_inward=True)
    if followers_only:      # a pending follower has not been let in yet
        follower_query = follower_query.filter_by(is_accepted=True)
    followers = follower_query.all()
    if not followers:
        return

    # send the amended copy of the Create to anyone who is following the User, but hasn't already received something
    # S7.2: a followers-only post's cc is its mentions; the followers collection in `to` already names its followers
    for follower in ([] if followers_only else followers):
        # remote_user_id is a nullable FK -- see the same shape in
        # app/shared/tasks/deletes.py. The None is kept out of .get(), which
        # SQLAlchemy warns may stop answering None, and `if user_details`
        # below remains the single decision this loop makes.
        user_details = (session.get(User, follower.remote_user_id)
                        if follower.remote_user_id else None)
        if user_details:
            create['cc'].append(user_details.public_url())

    # a followers-only post got no Announce, so a piefed follower host has nothing yet and is sent the Create too
    for instance in (follower_instances(user) if followers_only else user.following_instances()):
        if instance.domain not in domains_sent_to and instance.id != 1 and (followers_only or instance.software != 'piefed'):
            if can_deliver_to(user, instance):
                send_post_request(instance.inbox, create, user.private_key, user.public_url() + '#main-key')



""" JSON format
Move:
{
  'id':
  'type':
  'actor':
  'object':
  '@context':
  'audience':
  'to': []
  'cc': []
}
For Announce, remove @context from inner object, and use same fields except audience
"""


@celery.task
def move_post(send_async, user_id, old_community_id, new_community_id, post_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                if post and not post.deleted:
                    new_community = session.get(Community, new_community_id)
                    old_community = session.get(Community, old_community_id)
                    move_object(session, user_id, post, origin=old_community, target=new_community)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def move_object(session, user_id, object, origin, target):
    user = session.get(User, user_id)

    if isinstance(origin, Community) and isinstance(target, Community):
        community = origin
    else:
        raise TaskError('Unsupported origin or target')

    if community.local_only or community.private or not (community.instance_id and community.instance.online()):
        return
    if not is_open(object):     # Amendment B (S7.4): move_post refuses this; never federate one that got through
        return

    move_id = f"{current_app.config['SERVER_URL']}/activities/move/{gibberish(15)}"
    to = ["https://www.w3.org/ns/activitystreams#Public"]
    cc = [community.public_url()]
    move = {
      'id': move_id,
      'type': 'Move',
      'actor': user.public_url(),
      'object': f'{object.public_url()}',
      '@context': default_context(),
      'origin': origin.public_url(),
      'target': target.public_url(),
      'to': to,
      'cc': cc
    }

    if community.is_local():
        del move['@context']

        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        actor = community.public_url()
        cc = [community.ap_followers_url]
        announce = {
          'id': announce_id,
          'type': 'Announce',
          'actor': actor,
          'object': move,
          '@context': default_context(),
          'to': to,
          'cc': cc
        }
        for instance in community.following_instances():
            if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
    else:
        send_post_request(community.ap_inbox_url, move, user.private_key, user.public_url() + '#main-key')
