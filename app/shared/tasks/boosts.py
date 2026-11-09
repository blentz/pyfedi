"""A local person's boost of a post, federated (owner ruling 2026-11-08: "build local boosts"; plan W1.3, ruling M5).

A boost is an Announce from the person's own actor, as Mastodon's is: to Public, cc their followers and the post's
author, delivered to every remote instance holding an accepted follower of theirs and to the author's inbox when the
author is remote. Taking it back is an Undo of that Announce, whose id is the person's and the post's (activities/
announce/boost-<post id>), so the Undo names it without a row of its own. Only an open post outside a private or
local-only community is announced; refused_outbound refuses anything local-only on the way out as for every activity."""
from flask import current_app

from app import celery
from app.activitypub.signature import default_context, send_post_request
from app.federation_urls import activity_id
from app.local_only import refused_outbound
from app.models import Post, User, utcnow
from app.utils import ap_datetime, get_task_session, instance_banned, patch_db_session
from app.visibility import follower_instances, is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def announce_of(user: User, post: Post) -> dict:
    """The Announce `user` sends for `post`, without @context; its id is the same each time, for an Undo to name."""
    return {'id': activity_id('announce', user, f'boost-{post.id}'), 'type': 'Announce', 'actor': user.public_url(),
            'object': post.public_url(), 'published': ap_datetime(utcnow()), 'to': [PUBLIC],
            'cc': [user.followers_url(), post.author.public_url()]}


def boostable(post: Post) -> bool:
    """Whether a boost of `post` may reach other servers: published, open, and in an open community that federates."""
    community = post.community
    return not post.deleted and is_open(post) and not community.private and not community.local_only and \
        not post.local_only


def _inboxes(user: User, post: Post) -> list[str]:
    inboxes = [instance.inbox for instance in follower_instances(user)
               if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and
               not instance_banned(instance.domain)]
    author = post.author
    if not author.is_local() and author.follow_answer_inbox() and author.instance_id and author.instance.online() \
            and not instance_banned(author.instance.domain) and not user.has_blocked_instance(author.instance_id):
        inboxes.append(author.follow_answer_inbox())
    return list(dict.fromkeys(inboxes))


def send_boost(user_id: int, post_id: int, undo: bool) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            user, post = session.get(User, user_id), session.get(Post, post_id)
            if user is None or post is None or not boostable(post):
                return
            announce = announce_of(user, post)
            body = {'@context': default_context(), **announce}
            if undo:
                body = {'@context': default_context(), 'id': activity_id('undo', user), 'type': 'Undo',
                        'actor': user.public_url(), 'object': announce, 'to': announce['to'], 'cc': announce['cc']}
            for inbox in _inboxes(user, post):
                if not refused_outbound(body, inbox, session):
                    send_post_request(inbox, body, user.private_key, user.public_url() + '#main-key')
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def boost_task(user_id: int, post_id: int, undo: bool = False):
    with current_app.app_context():
        send_boost(user_id, post_id, undo)


def queue_boost(user_id: int, post_id: int, undo: bool = False) -> None:
    """Federate a boost or its Undo: in a Celery task, or at once in debug (as task_selector does)."""
    if current_app.debug:
        send_boost(user_id, post_id, undo)
    else:
        boost_task.delay(user_id, post_id, undo)
