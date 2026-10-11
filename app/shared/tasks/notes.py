import json
from app import cache, celery, db
from app.activitypub.signature import default_context, post_request, send_post_request
from app.constants import NOTIF_MENTION
from app.models import Community, CommunityJoinRequest, CommunityMember, Notification, Post, \
    PostReply, utcnow, User
from app.shared.tasks.pages import can_deliver_to, reply_recipients
from app.visibility import can_view, follower_instances, followers_only_addressing, is_open
from app.utils import user_banned_from_community, community_membership, gibberish, joined_communities, instance_banned, ap_datetime, \
    recently_upvoted_posts, recently_downvoted_posts, recently_upvoted_post_replies, \
    recently_downvoted_post_replies, get_recipient_language, get_task_session, patch_db_session

from flask import current_app
from flask_babel import _, force_locale, gettext


""" Reply JSON format
{
  'id':
  'url':
  'type':
  'attributedTo':
  'to': []
  'cc': []
  'tag': []
  'audience':
  'content':
  'mediaType':
  'source': {}
  'inReplyTo':
  'published':
  'updated':        (inner oject of Update only)
  'language': {}
  'contentMap':{}
  'distinguished'
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
def make_reply(send_async, reply_id, parent_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_reply(reply_id, parent_id, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def edit_reply(send_async, reply_id, parent_id):
    session = get_task_session()
    try:
        with patch_db_session(session):
            send_reply(reply_id, parent_id, edit=True, session=session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def send_reply(reply_id, parent_id, edit=False, session=None):
    reply = session.query(PostReply).filter_by(id=reply_id).one()
    user = reply.author
    if parent_id:
        parent = session.query(PostReply).filter_by(id=parent_id).one()
    else:
        parent = reply.post
    community = reply.community

    # The parent's author, then any users Mentioned in reply with @user@instance syntax, once each
    recipients = reply_recipients(reply, parent)

    # Notify any local users that have been Mentioned
    for recipient in recipients:
        if recipient.is_local() and recipient.id != parent.author.id and can_view(reply, recipient.id):
            if edit:
                existing_notification = session.query(Notification).filter(Notification.user_id == recipient.id, Notification.url == f"{current_app.config['SERVER_URL']}/comment/{reply.id}").first()
            else:
                existing_notification = None
            if not existing_notification:
                author = session.get(User, user.id)
                targets_data = {'gen':'0',
                                'post_id':reply.post_id,
                                'author_user_name': author.ap_id if author.ap_id else author.user_name,
                                'comment_id': reply.id,
                                'comment_body': reply.body
                                }
                with force_locale(get_recipient_language(recipient.id)):
                    notification = Notification(user_id=recipient.id, title=gettext(f"You have been mentioned in comment {reply.id}"),
                                                url=f"{current_app.config['SERVER_URL']}/comment/{reply.id}",
                                                author_id=user.id, notif_type=NOTIF_MENTION,
                                                subtype='comment_mention',
                                                targets=targets_data)
                    recipient.unread_notifications += 1
                    session.add(notification)
                    session.commit()

    if community.local_only or community.private or community.is_gone():   # interop C1 item 10: nothing goes to a gone Group
        return

    banned = user_banned_from_community(user.id, community.id)  # D995
    if banned:
        return

    # Amendment B (S7.3): a followers-only reply goes to the replier's followers, cc the people it answers or names
    # (R-FOCC: never the follower list), with no Public and no community audience. It never goes to the community,
    # so the community's host being offline, blocked or banned does not stop it.
    followers_only = not is_open(reply)
    if not followers_only:
        if not (community.instance_id and community.instance.online()):
            return
        if not community.is_local():
            if user.has_blocked_instance(community.instance.id) or instance_banned(community.instance.domain):
                return

    if followers_only:
        addressing = followers_only_addressing(user, recipients)
        to, cc = addressing['to'], addressing['cc']
    else:
        to = ["https://www.w3.org/ns/activitystreams#Public"]
        cc = [community.public_url()] + [recipient.public_url() for recipient in recipients]
    tag = []
    for recipient in recipients:
        tag.append({'href': recipient.public_url(), 'name': recipient.mention_tag(), 'type': 'Mention'})
    tag.extend(reply.tags_for_activitypub())
    language = {'identifier': reply.language_code(), 'name': reply.language_name()}
    content_map = {reply.language_code(): reply.body_html}
    source = {'content': reply.body, 'mediaType': 'text/markdown'}
    note = {
      'id': reply.public_url(),
      'url': reply.public_url(),
      'type': 'Note',
      'attributedTo': user.public_url(),
      'to': to,
      'cc': cc,
      'tag': tag,
      'audience': community.public_url(),
      'content': reply.body_html,
      'mediaType': 'text/html',
      'source': source,
      'inReplyTo': parent.public_url(),
      'published': ap_datetime(reply.posted_at),
      'language': language,
      'contentMap': content_map,
      'distinguished': reply.distinguished,
      'flair': user.community_flair(reply.community_id),
    }
    if edit:
        note['updated'] = ap_datetime(utcnow())

    activity = 'create' if not edit else 'update'
    create_id = f"{current_app.config['SERVER_URL']}/activities/{activity}/{gibberish(15)}"
    type = 'Create' if not edit else 'Update'
    create = {
      'id': create_id,
      'type': type,
      'actor': user.public_url(),
      'object': note,
      'to': to,
      'cc': cc,
      '@context': default_context(),
      'audience': community.public_url()
    }
    if followers_only:
        del note['audience']
        del create['audience']
        note['interactionPolicy'] = addressing['interactionPolicy']

    domains_sent_to = [current_app.config['SERVER_NAME']]

    # send the activity as an Announce if the community is local, or as a Create if not. A followers-only reply is
    # never Announced, and a remote community's inbox is not its audience either.
    if followers_only:
        pass
    elif community.is_local():
        del create['@context']

        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        cc = [community.ap_followers_url]
        announce = {
          'id': announce_id,
          'type': 'Announce',
          'actor': community.public_url(),
          'object': create,
          'to': to,
          'cc': cc,
          '@context': default_context()
        }
        for instance in community.following_instances():
            if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
                domains_sent_to.append(instance.domain)
    else:
        send_post_request(community.ap_inbox_url, create, user.private_key, user.public_url() + '#main-key')
        domains_sent_to.append(community.instance.domain)

    # send copy of the Create to anyone else Mentioned in reply, but not on an instance that's already sent to.
    if '@context' not in create:
        create['@context'] = default_context()
    for recipient in recipients:
        if recipient.instance.domain not in domains_sent_to:
            if followers_only and not can_deliver_to(user, recipient.instance):
                continue
            send_post_request(recipient.instance.inbox, create, user.private_key, user.public_url() + '#main-key')
            domains_sent_to.append(recipient.instance.domain)     # two people on one host get one delivery
    if followers_only:
        for instance in follower_instances(user):
            if instance.domain not in domains_sent_to and can_deliver_to(user, instance):
                send_post_request(instance.inbox, create, user.private_key, user.public_url() + '#main-key')
                domains_sent_to.append(instance.domain)


@celery.task
def choose_answer(send_async, post_reply_id, user_id):
    send_answer(post_reply_id, user_id, False)


@celery.task
def unchoose_answer(send_async, post_reply_id, user_id):
    send_answer(post_reply_id, user_id, True)


def send_answer(post_reply_id, user_id, is_undo):
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.get(User, user_id)
            post_reply = session.get(PostReply, post_reply_id)

            if post_reply.community.local_only or post_reply.community.private or not (post_reply.community.instance_id and post_reply.community.instance.online()):
                return

            answer_ap_id = f"{current_app.config['SERVER_URL']}/activities/answer/{gibberish(15)}"
            to = ["https://www.w3.org/ns/activitystreams#Public"]
            cc = [post_reply.community.public_url()]
            lock = {
              'id': answer_ap_id,
              'type': 'ChooseAnswer',
              'actor': user.public_url(),
              'object': post_reply.public_url(),
              '@context': default_context(),
              'audience': post_reply.community.public_url(),
              'to': to,
              'cc': cc
            }

            if is_undo:
                del lock['@context']
                undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/{gibberish(15)}"
                undo = {
                  'id': undo_id,
                  'type': 'Undo',
                  'actor': user.public_url(),
                  'object': lock,
                  '@context': default_context(),
                  'audience': post_reply.community.public_url(),
                  'to': to,
                  'cc': cc
                }

            if post_reply.community.is_local():
                if is_undo:
                    del undo['@context']
                    object=undo
                else:
                    del lock['@context']
                    object=lock

                announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
                actor = post_reply.community.public_url()
                cc = [post_reply.community.ap_followers_url]
                announce = {
                  'id': announce_id,
                  'type': 'Announce',
                  'actor': actor,
                  'object': object,
                  '@context': default_context(),
                  'to': to,
                  'cc': cc
                }
                for instance in post_reply.community.following_instances():
                    if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                        send_post_request(instance.inbox, announce, post_reply.community.private_key, post_reply.community.public_url() + '#main-key')
            else:
                payload = undo if is_undo else lock
                send_post_request(post_reply.community.ap_inbox_url, payload, user.private_key, user.public_url() + '#main-key')

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
