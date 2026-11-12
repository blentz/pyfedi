"""Pixelfed's stories for its official app (plan W1.4c; spec 2026-11-10-pixelfed-app-design §7, ruling X3 and rulings
S1-S4 of 2026-11-11) and PixelDroid (W1.4d, ruling S5), in its server's shapes (StoryApiV1Controller,
StoryIndexService::fetchCarouselNodes). A story is a microblog post with a picture from the last day, by the viewer or
someone they follow, that the viewer may read: nothing is stored for one but who saw it (S1, shown to nobody) and
whether its author expired it early (S2, the post stays). A comment is a chat to the author, through the chat's own
send (S3). Publishing (S5, reversing S4 now that PixelDroid publishes) uploads the photo as Mastodon's media are and
posts it to the author's microblog with no words, as POST /api/v1/statuses would: a story for a day, and a post."""
from datetime import timedelta

from flask import g, jsonify, request
from sqlalchemy import select

from app import db
from app.chat import membership
from app.chat.util import send_message
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, SRC_API
from app.mastodon import bp
from app.mastodon.compose import guarded, write_status
from app.mastodon.direct import may_message, whole_number
from app.mastodon.entities import absolute, acct, timestamp
from app.mastodon.instance import MAX_CHARACTERS
from app.mastodon.media import own_uploads, store_upload
from app.mastodon.timelines import readable
from app.models import Conversation, File, Post, StoryExpiry, StoryView, User, UserFollower
from app.oauth_server.protect import require_oauth
from app.reads.viewer import Restrictions
from app.utils import is_local_microblogs, utcnow

LIFETIME, KEPT = timedelta(hours=24), timedelta(days=2)
DURATION_MS = 10000   # Pixelfed's least: a story is shown for at least ten seconds
MAX_STORIES = 200     # the newest a carousel considers


def _authors(viewer_id: int):
    """The viewer and everyone they follow (an accepted follow of theirs), as Pixelfed's carousel takes them."""
    followed = select(UserFollower.remote_user_id).where(UserFollower.local_user_id == viewer_id,
                                                        UserFollower.is_inward.is_(False),
                                                        UserFollower.is_accepted.is_(True))
    return [viewer_id, *db.session.scalars(followed)]


def _picture(post: Post):
    pictures = [file for file in post.album_images() if file.view_url()]
    return pictures[0] if pictures else None


def _stories(viewer_id: int) -> list[Post]:
    """The viewer's stories, newest first: each a microblog post with a picture, of the last day, not expired early,
    by the viewer or someone they follow, that they may read and have not blocked (a person, community or instance,
    as a listing leaves out)."""
    expired, blocked = select(StoryExpiry.post_id), Restrictions(viewer_id)
    rows = Post.query.filter(Post.user_id.in_(_authors(viewer_id)), Post.deleted.is_(False),
                             Post.type.in_((POST_TYPE_IMAGE, POST_TYPE_GALLERY)),
                             Post.posted_at > utcnow() - LIFETIME, Post.id.not_in(expired),
                             Post.user_id.not_in(blocked.blocked_person_ids),
                             Post.community_id.not_in(blocked.blocked_community_ids),
                             Post.instance_id.not_in(blocked.blocked_instance_ids)) \
        .order_by(Post.posted_at.desc()).limit(MAX_STORIES)
    return [post for post in rows if (post.microblog or is_local_microblogs(post.community))
            and _picture(post) is not None and readable(post, viewer_id)]


def _story(viewer_id: int, value) -> Post | None:
    """The story `value` names among the viewer's, or None."""
    number = whole_number(value)
    return next((post for post in _stories(viewer_id) if post.id == number), None) if number else None


def _item(post: Post, is_author: bool, seen: set) -> dict:
    item = {'id': str(post.id), 'type': 'photo', 'url': absolute(_picture(post).view_url()), 'overlays': [],
            'duration': DURATION_MS, 'viewed': is_author or post.id in seen,
            'created_at': timestamp(post.posted_at)}
    if is_author:
        item['view_count'] = 0   # ruling S1: nobody is told who looked, nor how many
    return item


def _node(author: User, posts: list[Post], viewer_id: int, seen: set) -> dict:
    is_author = author.id == viewer_id
    stories = [_item(post, is_author, seen) for post in sorted(posts, key=lambda post: post.id)]
    return {'id': f"{'self' if is_author else 'pfs'}:{author.id}", 'username': author.user_name,
            'username_acct': acct(author), 'profile_id': str(author.id), 'avatar': absolute(author.avatar_image()),
            'is_author': is_author, 'stories': stories, 'url': author.public_url(),
            'hasViewed': all(story['viewed'] for story in stories)}


@bp.route('/api/v1.2/stories/carousel')
@require_oauth('read')
def stories_carousel():
    """Each author's stories, the viewer's own first, then the others by their newest story."""
    viewer_id = g.oauth_user.id
    stories = _stories(viewer_id)
    seen = _seen_ids(viewer_id, stories)
    by_author = _by_author(stories)
    order = sorted(by_author, key=lambda author_id: author_id != viewer_id)
    return jsonify({'nodes': [_node(by_author[author_id][0].author, by_author[author_id], viewer_id, seen)
                              for author_id in order]})


def _fields() -> dict:
    """The request's fields from its query, form and JSON body, as Pixelfed's server takes them: its official app
    sends JSON, PixelDroid a query."""
    body = request.get_json(silent=True)
    return {**request.args.to_dict(), **request.form.to_dict(), **(body if isinstance(body, dict) else {})}


def _seen_ids(viewer_id: int, stories: list[Post]) -> set:
    return set(db.session.scalars(select(StoryView.post_id).where(
        StoryView.user_id == viewer_id, StoryView.post_id.in_([post.id for post in stories]))))


def _by_author(stories: list[Post]) -> dict[int, list[Post]]:
    """The stories by author, the authors in the order of their newest story (`stories` are newest first)."""
    by_author: dict[int, list[Post]] = {}
    for post in stories:
        by_author.setdefault(post.user_id, []).append(post)
    return by_author


def _src(post: Post, viewer_id: int, seen: set) -> dict:
    """A story in the self-carousel's shape: its duration in seconds."""
    return {'id': str(post.id), 'pid': str(post.user_id), 'type': 'photo', 'src': absolute(_picture(post).view_url()),
            'duration': DURATION_MS // 1000, 'seen': post.user_id == viewer_id or post.id in seen,
            'created_at': timestamp(post.posted_at)}


@bp.route('/api/pixelfed/v1/stories/self-carousel')
@require_oauth('read')
def stories_self_carousel():
    """PixelDroid's carousel (ruling S5), Pixelfed's selfCarousel: one's own stories under `self`, oldest first, and
    each followed author's under `nodes`, those whose newest story is not yet seen first."""
    me = g.oauth_user
    stories = _stories(me.id)
    seen = _seen_ids(me.id, stories)
    by_author = _by_author(stories)
    own = by_author.pop(me.id, [])
    nodes = []
    for posts in by_author.values():
        author = posts[0].author
        nodes.append({'id': f'pfs:{author.id}', 'url': author.public_url(), 'seen': posts[0].id in seen,
                      'user': {'id': str(author.id), 'username': author.user_name, 'username_acct': acct(author),
                               'avatar': absolute(author.avatar_image()), 'local': author.is_local(),
                               'is_author': False},
                      'nodes': [_src(post, me.id, seen) for post in reversed(posts)]})
    nodes.sort(key=lambda node: node['seen'])
    return jsonify({'self': {'user': {'id': str(me.id), 'username': acct(me), 'avatar': absolute(me.avatar_image()),
                                      'local': True, 'is_author': True},
                             'nodes': [_src(post, me.id, seen) for post in reversed(own)]},
                    'nodes': nodes})


def _not_found():
    return jsonify({'error': 'Record not found'}), 404


@bp.route('/api/v1.1/stories/seen', methods=['POST'])
@require_oauth('write')
def story_seen():
    """Ruling S1: recorded for the viewer alone; one's own story records nothing, as Pixelfed's."""
    viewer_id = g.oauth_user.id
    story = _story(viewer_id, _fields().get('id'))
    if story is None:
        return _not_found()
    if story.user_id == viewer_id:
        return jsonify([])
    StoryView.query.filter(StoryView.user_id == viewer_id, StoryView.created_at < utcnow() - KEPT).delete()
    if db.session.get(StoryView, (viewer_id, story.id)) is None:
        db.session.add(StoryView(user_id=viewer_id, post_id=story.id))
    db.session.commit()
    return jsonify({'code': 200})


@bp.route('/api/v1.1/stories/self-expire/<int:post_id>', methods=['POST'])   # a story's id is its post's
@require_oauth('write')
def story_self_expire(post_id):
    """Ruling S2: one's own story leaves every carousel; the post stays."""
    story = _story(g.oauth_user.id, post_id)
    if story is None or story.user_id != g.oauth_user.id:
        return _not_found()
    StoryExpiry.query.filter(StoryExpiry.created_at < utcnow() - KEPT).delete()
    db.session.add(StoryExpiry(post_id=story.id))
    db.session.commit()
    return jsonify({'code': 200, 'msg': 'Successfully deleted'})


@bp.route('/api/v1.2/stories/viewers')
@require_oauth('read')
def story_viewers():
    """Ruling S1: one's own story's viewers are nobody, in Pixelfed's cursor page."""
    story = _story(g.oauth_user.id, request.args.get('sid'))
    if story is None or story.user_id != g.oauth_user.id:
        return _not_found()
    return jsonify({'data': [], 'links': {'next': None, 'prev': None}})


@bp.route('/api/v1.1/stories/comment', methods=['POST'])
@require_oauth('write')
def story_comment():
    """Ruling S3: the caption and a link to the post, as a chat message to the author: the conversation with them,
    found or started (a request of theirs unless they follow the commenter), through the chat's own send."""
    me, fields = g.oauth_user, _fields()
    story = _story(me.id, fields.get('sid'))
    if story is None or story.user_id == me.id:
        return _not_found()
    caption = fields.get('caption')
    if not isinstance(caption, str) or not caption.strip() or len(caption) > MAX_CHARACTERS:
        return jsonify({'error': f'A comment is text of 1 to {MAX_CHARACTERS} characters'}), 422
    author = story.author
    refusal = may_message(me, author)
    if refusal:
        return refusal
    conversation = Conversation.find_existing_conversation(recipient=author, sender=me) \
        or membership.start_conversation(me, author)
    for person in (me, author):
        membership.update_member(conversation.id, person.id, joined=True)
    send_message(f'{caption.strip()}\n\n{story.public_url()}', conversation.id, user=me, src=SRC_API)
    return jsonify({'code': 200, 'msg': 'Sent!'})


@bp.route('/api/v1.1/stories/add', methods=['POST'])
@require_oauth('write')
def story_add():
    """Ruling S5: the photo, uploaded as Mastodon's media are, until stories/publish posts it."""
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'error': 'The file field is required.'}), 422
    file = store_upload(upload)
    if not isinstance(file, File):
        return file
    db.session.commit()
    return jsonify({'code': 200, 'msg': 'Successfully added', 'media_id': str(file.id),
                    'media_url': absolute(file.view_url()), 'media_type': 'photo'})


@bp.route('/api/v1.1/stories/publish', methods=['POST'])
@require_oauth('write')
def story_publish():
    """Ruling S5: one's own upload, posted to one's microblog with no words, as POST /api/v1/statuses would. Its
    duration, replies and reactions are not kept: a story shows for ten seconds, and a reply to it is a chat (S3)."""
    media_id = _fields().get('media_id')
    if not own_uploads(g.oauth_user.id, [media_id]):
        return jsonify({'error': 'The selected media id is invalid.'}), 422

    def write():
        write_status({'status': '', 'media_ids': [str(media_id)]})
        return jsonify({'code': 200, 'msg': 'Successfully published'})
    return guarded(write)
