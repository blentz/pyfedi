"""Which posts go to which connected accounts (spec §5)."""
from urllib.parse import urlsplit

from flask_babel import _

from app import db
from app.connections import Service
from app.constants import POST_STATUS_PUBLISHED, VISIBILITY_PUBLIC
from app.models import Post, Site, SocialConnection, media_of

DROP = 'drop'   # never goes, and no row says so: the post is private, or not this service's shape


def uploaded_video(post: Post) -> bool:
    """The post carries a video uploaded here (not a link to one elsewhere)."""
    return str((media_of(post) or {}).get('mime', '')).startswith('video/')


def image_files(post: Post) -> list:
    """The post's pictures, in order: a gallery's files, or the one image of an image post or microblog."""
    if post.gallery_count:
        return list(post.gallery)
    image = post.image if post.image_id else None
    return [image] if image is not None and (image.file_path or image.source_url) else []


def _private_instance() -> bool:
    site = db.session.get(Site, 1)
    return bool(site and site.private_instance)


def refusal(post: Post, connection: SocialConnection, service: Service) -> str | None:
    """Why `post` does not go to `connection`: DROP, a reason shown to the author (the row is kept as skipped), or
    None when it goes."""
    community = post.community
    if (post.deleted or post.status != POST_STATUS_PUBLISHED or post.user_id != connection.user_id
            or not post.author.is_local() or post.visibility != VISIBILITY_PUBLIC or post.local_only
            or community.private or community.local_only or _private_instance()
            or not post.ap_id or (urlsplit(post.ap_id).hostname or '').endswith('.onion')):
        return DROP
    if not service.fits(post):
        return DROP
    if (post.nsfw or post.nsfl) and not service.takes_nsfw:
        return _('Skipped: NSFW posts are not sent to %(service)s.', service=service.label)
    return None
