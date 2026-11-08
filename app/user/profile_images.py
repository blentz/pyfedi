"""A person's avatar and banner, replaced by an upload: the web profile page's and the Mastodon API's
(`update_credentials`, plan W1.1) one way of doing it."""
from app import cache, db
from app.community.util import save_banner_file, save_icon_file
from app.models import File, User


def _replace(user: User, saved: File | None, column: str) -> bool:
    """`user`'s `column` file becomes `saved`, the old one removed once the new one is in. False when nothing was
    saved."""
    if not saved:
        return False
    old_id = getattr(user, f'{column}_id')
    setattr(user, column, saved)
    old = db.session.get(File, old_id) if old_id else None
    if old is not None:
        db.session.delete(old)
    db.session.commit()
    if old is not None:
        old.delete_from_disk()
    return True


def replace_avatar(user: User, upload) -> bool:
    """`upload` (a FileStorage with a filename) as `user`'s avatar; a 400 for one that is not an image."""
    return _replace(user, save_icon_file(upload, 'users'), 'avatar')


def replace_cover(user: User, upload) -> bool:
    """`upload` as `user`'s banner; a 400 for one that is not an image."""
    saved = save_banner_file(upload, 'users')
    if saved:
        cache.delete_memoized(User.cover_image, user)
    return _replace(user, saved, 'cover')
