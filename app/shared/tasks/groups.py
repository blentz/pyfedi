from app import celery
from app.activitypub.signature import default_context, post_request, send_post_request
from app.models import Community, User, utcnow
from app.federation_urls import activity_id, owner_base
from app.utils import ap_datetime, get_task_session, patch_db_session
from app.shared.community import get_comm_flair_list, comm_flair_ap_format

from flask import current_app


"""
Group JSON format:
{
  'id':
  'type':
  'attributedTo':
  'name':
  'preferredUsername':
  'sensitive':
  'published':
  'updated':
  'summary':
  'source': {}
  'icon':
  'image':
  'language': []
  'postingRestrictedToMods':
  'featured':
  'followers':
  'endpoints': {}
  'inbox':
  'outbox':
  'publicKey': {}
}
"""
""" Update / Announce JSON format
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


# this is only for local communities (local users can moderate remote communities, but the Update won't accepted if they edit them)

@celery.task
def edit_community(send_async, user_id, community_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                user = session.get(User, user_id)
                community = session.query(Community).filter_by(id=community_id).one()
                if community.local_only or community.private or not (community.instance_id and community.instance.online()):
                    return

                if not community.is_moderator(user):
                    return

                # Named on the owners' domains (plan C3), from the rows, as a Celery worker has no request: the
                # Update is the moderator's activity, the Group and its Announce the community's. A remote
                # community's home server ignores our copy of its Group: that copy is named on the sending
                # moderator's domain (SERVER_URL before C3)
                update_id = activity_id('update', user)
                base = owner_base(community if community.is_local() else user)
                group_id = f"{base}/c/{community.name}"
                group = {
                  'id': group_id,
                  'type': 'Group',
                  'attributedTo': group_id + '/moderators',
                  'name': community.title,
                  'preferredUsername': community.name,
                  'sensitive': True if community.nsfw or community.nsfl else False,
                  'published': ap_datetime(community.created_at),
                  'updated': ap_datetime(utcnow()),
                  'postingRestrictedToMods': community.restricted_to_mods,
                  'featured': group_id + '/featured',
                  'followers': group_id + '/followers',
                  'endpoints': {'sharedInbox': f"{base}/inbox"},
                  'inbox': group_id + '/inbox',
                  'outbox': group_id + '/outbox',
                  'publicKey':  {'id': group_id + '#main-key', 'owner': group_id, 'publicKeyPem': community.public_key}
                }
                if community.description_html:
                    group['summary'] = community.description_html
                if community.description:
                    group['source'] = {'content': community.description, 'mediaType': 'text/markdown'}
                if community.icon_id:
                    if community.icon_image().startswith('http'):
                        group["icon"] = {
                            "type": "Image",
                            "url": community.icon_image()
                        }
                    else:
                        group['icon'] = {
                            'type': 'Image',
                            'url': f"{base}{community.icon_image()}"
                        }
                if community.image_id:
                    if community.header_image().startswith('http'):
                        group["image"] = {
                            "type": "Image",
                            "url": community.header_image()
                        }
                    else:
                        group['image'] = {
                            'type': 'Image',
                            'url': f"{base}{community.header_image()}"
                        }
                language = []
                for community_language in community.languages:
                    language.append({'identifier': community_language.code, 'name': community_language.name})
                group['language'] = language
                
                group["tag"] = community.flair_for_ap(version=2)

                to = ['https://www.w3.org/ns/activitystreams#Public']
                cc = [community.public_url()]
                update = {
                  'id': update_id,
                  'type': 'Update',
                  'actor': user.public_url(),
                  'object': group,
                  'to': to,
                  'cc': cc,
                  'audience': community.public_url()
                }
                if community.is_local():
                    announce = {
                      'id': activity_id('announce', community),
                      'type': 'Announce',
                      'actor': community.public_url(),
                      'object': update,
                      'to': to,
                      'cc': cc,
                      '@context': default_context()
                    }

                    for instance in community.following_instances():
                        if instance.inbox and instance.online():
                            send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
                else:
                    send_post_request(community.ap_inbox_url, update, user.private_key, user.public_url() + '#main-key')
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
