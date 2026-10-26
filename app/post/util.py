from typing import List
from urllib.parse import urlparse
from datetime import datetime
import gzip
import orjson

from flask import current_app
from flask_login import current_user
from sqlalchemy import desc, asc, text, or_

from app import db, cache
from app.constants import VISIBILITY_PUBLIC, POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_VIDEO, POST_TYPE_POLL, \
    POST_STATUS_PUBLISHED
from app.federation_urls import local_object_for_url
from app.models import PostReply, Post, Community, User, Language, utcnow
from app.activitypub.serializers.microblog import body_hashtags
from app.local_only import content_local_only
from app.storefront import is_hosted_uri
from app.visibility import OPEN_VISIBILITIES, RestrictedReply, can_view, mark_restricted
from app.utils import blocked_or_banned_instances, blocked_users, is_video_hosting_site, get_request, silenced_instances, \
    MEDIA_MAX_BYTES, MEDIA_MAX_SECONDS, is_local_microblogs, community_membership_private, user_in_restricted_country


@cache.memoize(timeout=600)
def retrieve_archived_post(archived_url: str) -> dict:
    """Load archived post data from S3 or local disk"""
    if not archived_url:
        return None
        
    try:
        if archived_url.startswith('http'):
            # Load from S3 via HTTP
            response = get_request(archived_url, max_bytes=MEDIA_MAX_BYTES, max_seconds=MEDIA_MAX_SECONDS)
            if response.status_code == 200:
                # Try to decompress, but if it fails (i.e., Cloudflare already decompressed it during transit),
                # use the content directly
                try:
                    data = gzip.decompress(response.content)
                except gzip.BadGzipFile:
                    # Content was already decompressed by CDN/proxy
                    data = response.content
                return orjson.loads(data)
        else:
            # Load from local disk
            with gzip.open(archived_url, 'rb') as f:
                data = f.read()
                return orjson.loads(data)
    except Exception as e:
        current_app.logger.error(f"Failed to load archived data from {archived_url}: {e}")
        return None
    
    return None


UNKNOWN_PARENT = object()   # a stub's children: what their parent was is not in the archive


def convert_archived_replies_to_tree(archived_replies: list, post: Post) -> List[dict]:
    """Convert archived reply data back to the expected tree format using PostReply models"""
    if not archived_replies:
        return []
    
    def children_of(reply_data, parent=None):
        return [child for child in (create_real_reply(data, parent) for data in reply_data.get('replies', []))
                if child]

    def archived_local_only(reply_data, parent) -> bool:
        """Whether the archived reply is local-only. An archive written before local-only content existed has no
        such key: it is worked out as PostReply.new would, and where that cannot be done (no author row, or a parent
        that only a stub was kept of) the reply is taken to be local-only, so it is not shown to anonymous readers."""
        if 'local_only' in reply_data:
            return bool(reply_data['local_only'])
        author = db.session.get(User, reply_data['author_id']) if reply_data.get('author_id') else None
        if author is None or parent is UNKNOWN_PARENT:
            return True
        return content_local_only(author, post.community, parent, post)

    def create_real_reply(reply_data, parent):
        if reply_data.get('visibility', VISIBILITY_PUBLIC) not in OPEN_VISIBILITIES:
            if reply_data.get('id') is None:  # a malformed stub has no place in the tree: skipped, with its subtree
                return None
            return {
                'comment': RestrictedReply(reply_data['id'], reply_data.get('depth', 0), reply_data.get('parent_id'),
                                           reply_data.get('post_id', post.id), tuple(reply_data.get('path', []))),
                'restricted': True,
                'replies': children_of(reply_data, UNKNOWN_PARENT)
            }
        # Create a PostReply instance (not persisted to DB)
        post_reply = PostReply()
        post_reply.id = reply_data.get('id')
        post_reply.body = reply_data.get('body', '')
        post_reply.body_html = reply_data.get('body_html', '')
        # Parse datetime strings back to datetime objects
        if reply_data.get('posted_at'):
            post_reply.posted_at = datetime.fromisoformat(reply_data['posted_at'])
        if reply_data.get('edited_at'):
            post_reply.edited_at = datetime.fromisoformat(reply_data['edited_at'])
        post_reply.score = reply_data.get('score', 0)
        post_reply.ranking = reply_data.get('ranking', 0)
        post_reply.parent_id = reply_data.get('parent_id')
        post_reply.distinguished = reply_data.get('distinguished', False)
        post_reply.deleted = reply_data.get('deleted', False)
        post_reply.deleted_by = reply_data.get('deleted_by')
        post_reply.user_id = reply_data.get('user_id')
        post_reply.post_id = post.id
        post_reply.depth = reply_data.get('depth', 0)
        post_reply.language_id = reply_data.get('language_id')
        post_reply.replies_enabled = False
        post_reply.community_id = reply_data.get('community_id')
        post_reply.up_votes = reply_data.get('up_votes', 0)
        post_reply.down_votes = reply_data.get('down_votes', 0)
        post_reply.child_count = reply_data.get('child_count', 0)
        post_reply.path = reply_data.get('path', [])
        post_reply.answer = reply_data.get('answer', False)
        post_reply.visibility = reply_data.get('visibility', VISIBILITY_PUBLIC)
        post_reply.local_only = archived_local_only(reply_data, parent)   # mark_restricted withholds it from anonymous
        post_reply.reports = 0
        
        # Post relationship
        post_reply.post = post
        
        # Community relationship
        post_reply.community = post.community
        
        # Author from archived data (or fetch if we have user_id)
        #if reply_data.get('user_id'):
        #    post_reply.author = User.query.get(reply_data['user_id'])

        # Create a minimal User instance for display
        author = User()
        author.id = reply_data.get('author_id')
        author.title = reply_data.get('author_name', 'Unknown')
        author.indexable = reply_data.get('author_indexable', True)
        author.ap_id = reply_data.get('author_ap_id')
        author.ap_profile_id = reply_data.get('author_ap_profile_id')
        author.user_name = reply_data.get('author_user_name')
        author.site_domain_id = reply_data.get('author_site_domain_id')   # None, in an archive from before C1: the default
        author.deleted = reply_data.get('author_deleted')
        author.bot = reply_data.get('author_bot', False)
        author.banned = reply_data.get('author_banned', False)
        if reply_data.get('author_created'):
            author.created = datetime.fromisoformat(reply_data['author_created'])
        else:
            author.created = utcnow()
        author.instance_id = 1
        author.ap_domain = reply_data.get('author_ap_domain', '')
        author.reputation = reply_data.get('author_reputation', 1)

        post_reply.author = author
        
        # Language
        if reply_data.get('language_id'):
            post_reply.language = db.session.get(Language, reply_data['language_id'])
        
        return {
            'comment': post_reply,
            'replies': children_of(reply_data, post_reply)
        }
    
    return children_of({'replies': archived_replies})


def find_comment_branch(tree: list, comment_id: int) -> list:
    """The branch of a reply tree (convert_archived_replies_to_tree's) whose root is comment_id: [] when it is not
    in the tree"""
    for item in tree:
        if item['comment'].id == comment_id:
            return [item]
        found = find_comment_branch(item['replies'], comment_id)
        if found:
            return found
    return []


# replies to a post, in a tree, sorted by a variety of methods
def post_replies(post: Post, sort_by: str, viewer: User, db_only=False) -> List[PostReply]:
    # If post is archived, load from archived data
    if post.archived and db_only is False:
        archived_data = retrieve_archived_post(post.archived)
        if archived_data and 'replies' in archived_data:
            archived_replies = convert_archived_replies_to_tree(archived_data['replies'], post)
            return mark_restricted(archived_replies, viewer.id if viewer else None)

    comments = db.session.query(PostReply).filter_by(post_id=post.id)
    if viewer:
        instance_ids = blocked_or_banned_instances(viewer.id)
        if instance_ids:
            comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        if viewer.ignore_bots == 1:
            comments = comments.filter(PostReply.from_bot == False)
        blocked_accounts = blocked_users(viewer.id)
        if blocked_accounts:
            comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))
        # `is_moderator(viewer)`, not `is_moderator()`. With no argument the
        # method falls back to `current_user.get_id()` -- the WEB session --
        # which is not who `viewer` is when the API calls this: every
        # authenticated API reader was treated as a non-moderator and had the
        # hide threshold applied regardless, and with no request context at
        # all (a direct call, as the tests make) `current_user` is None and
        # the line is an AttributeError. The web callers pass `current_user`
        # as `viewer`, so naming it changes nothing for them.
        if viewer.reply_hide_threshold and not (viewer.is_admin_or_staff() or post.community.is_moderator(viewer)):
            comments = comments.filter(or_(PostReply.score > viewer.reply_hide_threshold, PostReply.collapsible == False))
        if viewer.read_language_ids and len(viewer.read_language_ids) > 0:
            comments = comments.filter(
                or_(PostReply.language_id.in_(tuple(viewer.read_language_ids)), PostReply.language_id == None))
    else:
        if instance_ids := silenced_instances():
            comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        comments.filter(PostReply.score > -20)
        if not db_only:  # no viewer: a local-only reply, and so all its replies, are left out (spec §3.4)
            comments = comments.filter(PostReply.local_only.is_(False))

    if sort_by == 'hot':
        comments = comments.order_by(desc(PostReply.ranking))
    elif sort_by == 'top':
        comments = comments.order_by(desc(PostReply.score))
    elif sort_by == 'new':
        comments = comments.order_by(desc(PostReply.posted_at))
    elif sort_by == 'old':
        comments = comments.order_by(asc(PostReply.posted_at))

    comments = comments.limit(2000)  # paginating indented replies is too hard so just get the first 2000.

    all_comments = comments.all()
    comments_dict = {comment.id: {'comment': comment, 'replies': []} for comment in all_comments}

    for comment in all_comments:
        if comment.parent_id is not None:
            parent_comment = comments_dict.get(comment.parent_id)
            if parent_comment:
                parent_comment['replies'].append(comments_dict[comment.id])

    tree = [comment for comment in comments_dict.values() if comment['comment'].parent_id is None]
    if db_only:
        return tree  # archive_post reads every reply, unmarked, and decides what to store
    return mark_restricted(tree, viewer.id if viewer else None)


def get_comment_branch(post: Post, comment_id: int, sort_by: str, viewer: User) -> List[PostReply]:
    # If post is archived, load from archived data
    if post.archived:
        archived_data = retrieve_archived_post(post.archived)
        if archived_data and 'replies' in archived_data:
            # The whole thread, then the branch: a reply the archive holds no 'local_only' for is worked out from
            # its real parent, and the branch's root would otherwise have none (and be shown when it should not)
            branch = find_comment_branch(convert_archived_replies_to_tree(archived_data['replies'], post), comment_id)
            return mark_restricted(branch, viewer.id if viewer else None)
    
    # Fetch the specified parent comment and its replies
    parent_comment = db.session.get(PostReply, comment_id)
    if parent_comment is None:
        return []

    comments = PostReply.query.filter(PostReply.post_id == post.id)
    if viewer:
        instance_ids = blocked_or_banned_instances(viewer.id)
        if instance_ids:
            comments = comments.filter(or_(PostReply.instance_id.not_in(instance_ids), PostReply.instance_id == None))
        if viewer.ignore_bots == 1:
            comments = comments.filter(PostReply.from_bot == False)
        blocked_accounts = blocked_users(viewer.id)
        if blocked_accounts:
            comments = comments.filter(PostReply.user_id.not_in(blocked_accounts))
        # `is_moderator(viewer)`, not `is_moderator()`. With no argument the
        # method falls back to `current_user.get_id()` -- the WEB session --
        # which is not who `viewer` is when the API calls this: every
        # authenticated API reader was treated as a non-moderator and had the
        # hide threshold applied regardless, and with no request context at
        # all (a direct call, as the tests make) `current_user` is None and
        # the line is an AttributeError. The web callers pass `current_user`
        # as `viewer`, so naming it changes nothing for them.
        if viewer.reply_hide_threshold and not (viewer.is_admin_or_staff() or post.community.is_moderator(viewer)):
            comments = comments.filter(PostReply.score > viewer.reply_hide_threshold)
        if viewer.read_language_ids and len(viewer.read_language_ids) > 0:
            comments = comments.filter(
                or_(PostReply.language_id.in_(tuple(viewer.read_language_ids)), PostReply.language_id == None))
    else:
        comments.filter(PostReply.score > -20)
        comments = comments.filter(PostReply.local_only.is_(False))  # no viewer: as post_replies

    if sort_by == 'hot':
        comments = comments.order_by(desc(PostReply.ranking))
    elif sort_by == 'top':
        comments = comments.order_by(desc(PostReply.score))
    elif sort_by == 'new':
        comments = comments.order_by(desc(PostReply.posted_at))
    elif sort_by == 'old':
        comments = comments.order_by(asc(PostReply.posted_at))

    comments_dict = {comment.id: {'comment': comment, 'replies': []} for comment in comments.all()}

    for comment in comments:
        if comment.parent_id is not None:
            parent_comment = comments_dict.get(comment.parent_id)
            if parent_comment:
                parent_comment['replies'].append(comments_dict[comment.id])

    tree = [comment for comment in comments_dict.values() if comment['comment'].id == comment_id]
    return mark_restricted(tree, viewer.id if viewer else None)


# The number of replies a post has
def get_post_reply_count(post_id) -> int:
    return db.session.execute(
        text('SELECT COUNT(*) as c FROM "post_reply" WHERE post_id = :post_id AND deleted is false'),
        {'post_id': post_id}).scalar()


def tags_to_string(post: Post, exclude=()) -> str:
    names = [tag.display_as for tag in post.tags if tag.name not in exclude]
    if names:
        return ', '.join(names)


def form_tags_string(post: Post) -> str:
    """The tags field of a form filled from `post`. A local microblogs post leaves out its body hashtags: saving
    stores them again from the body (spec 2026-10-14 §10 A12), and filling them in would make them explicit tags
    that taking the #word out of the body could no longer drop."""
    body_names = body_hashtags(post.body_html or '') if is_local_microblogs(post.community) else ()
    return tags_to_string(post, exclude=body_names)


def flair_to_string(post: Post) -> str:
    if len(post.flair) > 0:
        return ', '.join([flair.flair for flair in post.flair])     # flair flair flair flair flfl flfl flflflfl


def body_has_no_archive_link(body):
    if body:
        return 'https://archive.' not in body and 'https://12ft.io' not in body and 'https://removepaywalls.com' not in body
    else:
        return True


def url_needs_archive(url) -> bool:
    paywalled_sites = ['washingtonpost.com', 'nytimes.com', 'wsj.com', 'economist.com', 'ft.com', 'telegraph.co.uk',
                       'bild.de', 'theatlantic.com', 'lemonde.fr', 'nzherald.co.nz', 'theverge.com', 'reuters.com',
                       'hilltimes.com']
    if url:
        try:
            parsed_url = urlparse(url)
            hostname = parsed_url.hostname.lower()
            if hostname.startswith('www.'):
                hostname = hostname[4:]
        except ValueError:
            # urlparse refuses some netlocs outright: an unbalanced IPv6 bracket
            # ('https://[::1/x'), an address with two '::' runs, or a host that
            # fails its NFKC confusability check. A URL with no determinable
            # host is not one of the paywalled sites below.
            #
            # This clause is explicit rather than folded into the AttributeError
            # one below, because the two describe different failures: this one
            # is "urlparse would not read the string at all", that one is "it
            # read it and found no host". Covered by
            # tests/test_urlparse_valueerror_guards.py.
            return False
        except AttributeError:
            # `parsed_url.hostname` is None for a string with no authority --
            # 'not-a-url' parses to path='not-a-url', netloc='' -- and
            # None.lower() is AttributeError. That is the only exception the
            # two lines above can raise once urlparse has returned: hostname is
            # otherwise a str, on which .startswith() and the slice cannot
            # fail.
            #
            # This used to be a bare `except:`, carrying a comment calling it "a
            # separately reported defect". IT WAS NEVER REPORTED -- the two
            # reported bare clauses were fixup_url's, both narrowed in 1d1f25be
            # -- so the comment asserted a report that did not exist. Narrowed
            # here rather than re-labelled, for the reason 1d1f25be documented
            # at length: a bare `except:` swallows KeyboardInterrupt and
            # SystemExit, and it swallows the test harness's own errors, which
            # makes the region it wraps resistant to mutation testing.
            # tests/test_url_needs_archive.py::TestHostlessUrlReachesTheGuards
            # covers both arms.
            return False
        if hostname == 'nytimes.com' and 'unlocked_article_code' in url:
            return False
        if hostname == 'theatlantic.com' and 'gift' in url:
            return False
        return hostname in paywalled_sites
    else:
        return False


def generate_archive_link(url) -> bool:
    return 'https://www.removepaywall.com/search?url=' + url


# Forms like the cross post form need the type for the url
def post_type_to_form_url_type(post_type: int, post_url: str):
    if post_type == POST_TYPE_LINK or is_video_hosting_site(post_url):
        return 'link'
    elif post_type == POST_TYPE_IMAGE:
        return 'image'
    elif post_type == POST_TYPE_VIDEO:
        return 'video'
    elif post_type == POST_TYPE_POLL:
        return 'poll'
    else:
        return ''


NEEDS_LOGIN = 'login'   # post_refusal's answer for an anonymous reader of NSFW/NSFL content: the page sends them to log in


def deleted_post_state(post: Post, viewer: User | None = None) -> str:
    """How the post page treats a post of a banned community or a deleted post: 'live' (neither), 'author' (deleted by
    its author: shown, with a notice), 'staff' (shown to a moderator, staff or the author, with a notice) or 'gone'
    (404 for everyone else). `viewer` is the reader, the session's by default."""
    if not (post.community.banned or post.deleted):
        return 'live'
    if post.deleted_by == post.user_id:
        return 'author'
    who = current_user if viewer is None else viewer
    if who.is_authenticated and (post.community.is_moderator(viewer) or who.is_admin_or_staff()
                                 or who.id == post.user_id):
        return 'staff'
    return 'gone'


def post_refusal(post: Post, viewer: User | None = None) -> int | str | None:
    """The one place the post page's refusals are decided, in the order `show_post` applies them: 404 for a deleted
    post or one the reader may not see (followers-only, unpublished, local-only), `NEEDS_LOGIN` for an anonymous
    reader of NSFW/NSFL content, 403 for a private community or a restricted country, else None. `show_post` calls
    this, and a link to a post asks it too, so a post nobody could open is never hinted at. `viewer` is the reader
    (an API caller, say); by default the session's."""
    community = post.community
    who = current_user if viewer is None else viewer
    if deleted_post_state(post, viewer) == 'gone':
        return 404
    if not can_view(post, who.get_id()):   # 404, not 403, so the answer does not confirm the post exists
        return 404
    if who.is_anonymous:
        if current_app.config['CONTENT_WARNING']:
            if post.nsfl:
                return NEEDS_LOGIN
        elif post.nsfw or post.nsfl:
            return NEEDS_LOGIN
        if community.private:
            return 403
    else:
        if (post.nsfw or post.nsfl) and user_in_restricted_country(who):
            return 403
        if community.private and community.id not in community_membership_private(who.id):
            return 403
    if post.status < POST_STATUS_PUBLISHED and (who.is_anonymous or not (
            who.id == post.user_id or community.is_moderator(viewer) or who.is_admin_or_staff())):
        return 404
    return None


def hosted_post_for_viewer(uri: str, viewer: User | None = None) -> Post | None:
    """The local post a URL on one of our own domains names, when the logged-in viewer may open it, else None.
    Read from the database and never fetched: one server answers for every hosted domain. The caller sends the
    viewer to the post's page on the domain they are browsing, so the peer gate (`post_served_to_any_peer`) is not
    this lookup's business; what the page enforces is."""
    found = local_object_for_url(uri)
    return found if isinstance(found, Post) and post_refusal(found, viewer) is None else None
