from app import celery, db
from app.activitypub.serializers import peertube_video, uses_peertube_video
from app.activitypub.signature import default_context, send_post_request
from app.constants import NOTIF_REPORT, NOTIF_REPORT_ESCALATION
from app.models import Community, Instance, Post, PostReply, User, UserFollower, File, Notification, ChatMessage
from app.utils import gibberish, instance_banned, get_task_session, patch_db_session
from app.shared.tasks.pages import can_deliver_to, mentioned_users
from app.visibility import follower_instances, followers_only_reply_instances, is_open

from flask import current_app
from sqlalchemy import Integer


""" JSON format
Delete:
{
  'id':
  'type':
  'actor':
  'object':
  'summary':    (if deleted by mod / admin)
  '@context':
  'audience':
  'to': []
  'cc': []
}
For Announce, remove @context from inner object, and use same fields except audience
"""


@celery.task
def delete_reply(send_async, user_id, reply_id, reason=None):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                reply = session.query(PostReply).filter_by(id=reply_id).one()
                delete_object(user_id, reply, reason=reason, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def restore_reply(send_async, user_id, reply_id, reason=None):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                reply = session.query(PostReply).filter_by(id=reply_id).one()
                delete_object(user_id, reply, is_restore=True, reason=reason, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def delete_post(send_async, user_id, post_id, reason=None):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                delete_object(user_id, post, is_post=True, reason=reason, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def restore_post(send_async, user_id, post_id, reason=None):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                delete_object(user_id, post, is_post=True, is_restore=True, reason=reason, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def delete_community(send_async, user_id, community_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                community = session.query(Community).filter_by(id=community_id).one()
                delete_object(user_id, community, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def restore_community(send_async, user_id, community_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                community = session.query(Community).filter_by(id=community_id).one()
                delete_object(user_id, community, is_restore=True, session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _video_create(post) -> dict:
    video = peertube_video(post)
    return {'id': f"{current_app.config['SERVER_URL']}/activities/create/{gibberish(15)}", 'type': 'Create',
            'actor': post.author.public_url(), 'object': video, 'to': video['to'], 'cc': video['cc'],
            '@context': default_context()}


def delete_object(user_id, object, is_post=False, is_restore=False, reason=None, session=None):
    user = session.get(User, user_id)
    if isinstance(object, Community):
        community = object
    else:
        community = object.community

    # Amendment B (S7.2, S7.3): the Delete of a followers-only object goes where the object went -- the author's
    # followers, the author it replies to and the people it mentions -- with no Public, and never to a community's
    # followers. That holds in a remote community too when the author is local: we sent it, not the community (a
    # followers-only reply is never given to the community's inbox). Only a remote author's followers-only object in
    # a remote community takes the ordinary path: the community is its home (R-a). R-FODELETE: the audience is the
    # author's, so a moderator's or admin's removal is sent as the author too (this instance is authoritative for its
    # own accounts; Mastodon honours only the owner's Delete), never to the remover's own followers, and its reason
    # stays local.
    followers_only = not is_open(object) and (community.is_local() or object.author.is_local())
    if followers_only:
        user = object.author
        reason = None

    # local_only communities can also be used to send activity to User Followers (only applies to posts, not comments)
    # return now though, if there aren't any
    if not is_post and community.local_only:
        return
    followers = session.query(UserFollower).filter_by(local_user_id=user.id).all()
    if not followers and community.local_only:
        return

    if community.private or community.is_gone():   # interop C1 item 10: nothing goes to a gone Group
        return
    if not followers_only and not (community.instance_id and community.instance.online()):
        return

    # commented out because surely we still want to be able to delete stuff in banned/blocked places?
    #banned = CommunityBan.query.filter_by(user_id=user_id, community_id=community.id).first()
    #if banned:
    #    return
    #if not community.is_local():
    #    if user.has_blocked_instance(community.instance.id) or instance_banned(community.instance.domain):
    #        return

    delete_id = f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}"
    if followers_only:
        recipients = mentioned_users(object, allow_fetch=False)
        to = [user.followers_url()]
        cc = [recipient.public_url() for recipient in recipients]
        if not is_post:     # S7.3: a reply also named the author it answers, as send_reply addressed it
            cc = [object.to()] + [actor for actor in cc if actor != object.to()]
    else:
        to = ["https://www.w3.org/ns/activitystreams#Public"]
        cc = [community.public_url()]
    delete = {
      'id': delete_id,
      'type': 'Delete',
      'actor': user.public_url(),
      'object': object.public_url(),
      '@context': default_context(),
      'audience': community.public_url(),
      'to': to,
      'cc': cc
    }
    if reason:
        delete['summary'] = reason
    if followers_only:
        del delete['audience']

    if is_restore:
        del delete['@context']
        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/{gibberish(15)}"
        undo = {
          'id': undo_id,
          'type': 'Undo',
          'actor': user.public_url(),
          'object': delete,
          '@context': default_context(),
          'audience': community.public_url(),
          'to': to,
          'cc': cc
        }
        if followers_only:
            del undo['audience']

    domains_sent_to = []

    # a local_only community federates nothing itself; only the author's followers (below) are reached. Nor does a
    # followers-only object (E9): it never reached the community's followers, nor a remote community's inbox when its
    # author is local, so its Delete and Undo do not either. Anything else in a remote community has that community
    # as its home, which already holds it, so its inbox is still sent them (R-a).
    if community.local_only or followers_only:
        pass
    elif community.is_local():
        if is_restore:
            del undo['@context']
            object_json=undo
        else:
            del delete['@context']
            object_json=delete

        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        actor = community.public_url()
        cc = [community.ap_followers_url]
        announce = {
          'id': announce_id,
          'type': 'Announce',
          'actor': actor,
          'object': object_json,
          '@context': default_context(),
          'to': to,
          'cc': cc
        }
        video = is_post and uses_peertube_video(object)   # spec §10 A15.6: PeerTube takes a video's Delete only from its channel's account
        owner = session.get(User, community.user_id) if video and community.user_id else None
        if owner is not None and (not owner.is_local() or owner.deleted):   # only a live local owner can sign
            owner = None
        for instance in community.following_instances():
            if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                if video and instance.software == 'peertube':
                    if is_restore:      # PeerTube has no Undo(Delete): the video is sent again
                        send_post_request(instance.inbox, _video_create(object), object.author.private_key,
                                          object.author.public_url() + '#main-key')
                    elif owner is None:     # PeerTube refuses a community's or anyone else's Delete: leave the instance unsent
                        current_app.logger.warning(f'Delete of video {object.id} in community {community.id} not sent to '
                                                   f'{instance.domain}: the community has no live local owner to sign it')
                        continue
                    else:
                        send_post_request(instance.inbox, dict(delete, actor=owner.public_url(),
                                                               **{'@context': default_context()}),
                                          owner.private_key, owner.public_url() + '#main-key')
                    domains_sent_to.append(instance.domain)
                    continue
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
                domains_sent_to.append(instance.domain)
    else:
        payload = undo if is_restore else delete
        send_post_request(community.ap_inbox_url, payload, user.private_key, user.public_url() + '#main-key')
        domains_sent_to.append(community.instance.domain)


    if followers_only:
        # only a local author's instance speaks for the audience; a remote author's post or reply is theirs to delete.
        # A reply went to the replier's followers, the parent's author and its mentions (S7.3); the parent's host
        # comes from followers_only_reply_instances.
        if user.is_local():
            payload = undo if is_restore else delete
            audience = follower_instances(user) if is_post else followers_only_reply_instances(object)
            for instance in audience + [recipient.instance for recipient in recipients]:
                if instance.id != 1 and instance.domain not in domains_sent_to and can_deliver_to(user, instance):
                    send_post_request(instance.inbox, payload, user.private_key, user.public_url() + '#main-key')
                    domains_sent_to.append(instance.domain)
    elif is_post and followers and not reason:
        payload = undo if is_restore else delete
        for follower in followers:
            # remote_user_id is a nullable FK, so a follower row can carry no
            # account at all. The None is kept OUT of .get() rather than handed
            # to it: SQLAlchemy warns that a fully NULL primary key identity
            # "may raise an error in a future release", so relying on the None
            # it answers today is relying on a deprecation. The existing
            # `if user_details` below is still the one decision, which keeps
            # the skip on a branch a test can reach.
            user_details = (session.get(User, follower.remote_user_id)
                            if follower.remote_user_id else None)
            if user_details:
                payload['cc'].append(user_details.public_url())
        instances = session.query(Instance).join(User, User.instance_id == Instance.id).join(UserFollower, UserFollower.remote_user_id == User.id)
        instances = instances.filter(UserFollower.local_user_id == user.id).filter(Instance.gone_forever == False, Instance.dormant == False)
        for instance in instances:
            if instance.inbox and instance.domain not in domains_sent_to:
                send_post_request(instance.inbox, payload, user.private_key, user.public_url() + '#main-key')

    # remove any notifications about deleted posts
    if is_post:
        notifs = session.query(Notification).filter(Notification.targets.op("->>")("post_id").cast(Integer) == object.id)
        for notif in notifs:
            # dont delete report notifs
            if notif.notif_type == NOTIF_REPORT or notif.notif_type == NOTIF_REPORT_ESCALATION:
                continue
            session.delete(notif)
        session.commit()



@celery.task
def delete_posts_with_blocked_images(post_ids, user_id, send_async):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                for post_id in post_ids:
                    post = session.get(Post, post_id)
                    if post:
                        if post.url:
                            post.calculate_cross_posts(delete_only=True)
                        post.deleted = True
                        post.deleted_by = user_id
                        post.author.post_count -= 1
                        post.community.post_count -= 1
                        if post.image_id:
                            file = session.get(File, post.image_id)
                            file.delete_from_disk()
                        for gallery_file in post.gallery:  # an album's other images may be the blocked one
                            gallery_file.delete_from_disk()
                        session.commit()

                        delete_object(user_id, post, is_post=True, reason='Contains blocked image', session=session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# deleting PMs will get a new function: I can't be bothered reworking delete_object to remove community logic for such an obscure activity
@celery.task
def delete_pm(send_async, message_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                message = session.query(ChatMessage).filter_by(id=message_id).one()
                recipient = session.query(User).filter_by(id=message.recipient_id).one()
                delete_message(message, recipient)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def restore_pm(send_async, message_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                message = session.query(ChatMessage).filter_by(id=message_id).one()
                recipient = session.query(User).filter_by(id=message.recipient_id).one()
                delete_message(message, recipient, is_restore=True)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def delete_message(message, recipient, is_restore=False):
    if recipient.is_local():
        return

    delete_id = f"{current_app.config['SERVER_URL']}/activities/delete/{gibberish(15)}"
    delete = {
      'id': delete_id,
      'type': 'Delete',
      'actor': message.sender.public_url(),
      'object': message.ap_id,
      '@context': default_context(),
      'to': recipient.public_url()
    }

    if is_restore:
        del delete['@context']
        undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/{gibberish(15)}"
        undo = {
          'id': undo_id,
          'type': 'Undo',
          'actor': message.sender.public_url(),
          'object': delete,
          '@context': default_context(),
          'to': recipient.public_url()
        }

    payload = undo if is_restore else delete
    send_post_request(recipient.ap_inbox_url, payload, message.sender.private_key, message.sender.public_url() + '#main-key')
