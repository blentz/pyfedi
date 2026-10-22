from app import celery
from app.activitypub.signature import default_context, send_post_request
from app.federation_urls import activity_id
from app.models import Post, PostReply, User, Instance
from app.utils import get_task_session, patch_db_session

from flask import current_app


""" JSON format
Flag:
{
  'id':
  'type':
  'actor':
  'object':
  '@context':
  'audience':
  'to': []
  'summary':
}
"""


@celery.task
def report_reply(send_async, user_id, reply_id, summary, instance_ids):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                reply = session.query(PostReply).filter_by(id=reply_id).one()
                report_object(session, user_id, reply, summary, instance_ids)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@celery.task
def report_post(send_async, user_id, post_id, summary, instance_ids):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                report_object(session, user_id, post, summary, instance_ids)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def report_object(session, user_id, object, summary, instance_ids):
    user = session.get(User, user_id)
    community = object.community
    if community.local_only or community.private or community.is_gone() or \
            not (community.instance_id and community.instance.online()):   # is_gone: interop C1 item 10
        return

    flag_id = activity_id('flag', user)
    to = [community.public_url()]
    flag = {
      'id': flag_id,
      'type': 'Flag',
      'actor': user.public_url(),
      'object': object.public_url(),
      '@context': default_context(),
      'audience': community.public_url(),
      'to': to,
      'summary': summary
    }

    instances = session.query(Instance).filter(Instance.id.in_(instance_ids), Instance.dormant == False,
                                               Instance.gone_forever == False)
    for instance in instances:
        if instance.inbox is not None:
            send_post_request(instance.inbox, flag, user.private_key, user.public_url() + '#main-key')


@celery.task
def report_chat(send_async, user_id, reported_id, message_ap_ids, summary):
    """D761: a reported conversation is flagged to the reported member's instance, the object being that
    member's messages, in the Flag shape report_object sends for a post or reply. The object is the reported
    account's actor followed by the message ids, as Mastodon and Lemmy send and read a report (D761 follow-up)."""
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = session.get(User, user_id)
                reported = session.get(User, reported_id)
                instance = reported.instance
                if not message_ap_ids or instance is None or instance.inbox is None or instance.dormant or \
                        instance.gone_forever:
                    return
                flag = {
                    'id': activity_id('flag', user),
                    'type': 'Flag',
                    'actor': user.public_url(),
                    'object': [reported.ap_profile_id or reported.public_url(), *message_ap_ids],
                    '@context': default_context(),
                    'to': [reported.public_url()],
                    'summary': summary
                }
                send_post_request(instance.inbox, flag, user.private_key, user.public_url() + '#main-key')
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
