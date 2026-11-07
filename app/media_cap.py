"""The media cap: a remote post's local image copies last `Site.remote_image_cache_days` from the post's arrival, and
pages then load the image from `source_url`. 0 or NULL turns the cap off (owner rulings 2026-11-07).

Ruling on ownership: a row from before `File.file_written_by_us` cannot prove we wrote its file, and `delete_from_disk`
keeps such a file. Here the rule is narrower and the file goes when it is inside `media/posts` and no other record
names it (`File.evict_local_copies`), because otherwise those files would outlive the cap for good."""
import os
import time
from datetime import timedelta

from flask import current_app
from sqlalchemy import and_, exists, or_, select

from app import celery, db
from app.models import (File, MEDIA_ROOT, Post, Site, _local_owners, flush_cdn_cache, is_hosted_url, is_local_url,
                        named_by_any_other_record, post_file, utcnow)
from app.relays.expiry import _local_user_ids
from app.storefront import acting_as_default
from app.utils import get_task_session, patch_db_session

MEDIA_CAP_BATCH = 500
MEDIA_CAP_SECONDS = 600
ORPHAN_SWEEP_SECONDS = 3600
ORPHAN_MIN_AGE_SECONDS = 86400   # younger may have a record that is not committed yet
# Only pictures: `media/posts` also holds local posts' video and audio (store_uploaded_media), named by a post's url
ORPHAN_EXTENSIONS = {'.avif', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp'}


def _posts_using(condition):
    """Whether any post matching `condition` uses the File of the outer query, as its image or in its gallery."""
    return or_(exists().where(Post.image_id == File.id, condition),
               exists().where(post_file.c.file_id == File.id, Post.id == post_file.c.post_id, condition))


def _candidates(cutoff, last_id):
    local = _local_user_ids()
    old_and_remote = and_(Post.created_at < cutoff, Post.user_id.notin_(local))
    recent_or_local = or_(Post.created_at >= cutoff, Post.user_id.in_(local))
    return File.query.filter(
        File.id > last_id, or_(File.file_path.isnot(None), File.thumbnail_path.isnot(None)),
        File.source_url.like('http%'), _posts_using(old_and_remote), ~_posts_using(recent_or_local),
    ).order_by(File.id).limit(MEDIA_CAP_BATCH).all()


def _has_an_outside_source(file: File) -> bool:
    """A page falling back to a `source_url` on this server would load the very file just removed."""
    return not (is_local_url(file.source_url) or is_hosted_url(file.source_url))


@celery.task
@acting_as_default()   # instance-level: the default domain's, whoever queued it
def evict_old_post_media():
    session = get_task_session()
    evicted = failed = 0
    cache_urls = []
    try:
        with patch_db_session(session):
            days = session.get(Site, 1).remote_image_cache_days
            if not days or days <= 0:
                return {'evicted': 0, 'failed': 0}
            cutoff = utcnow() - timedelta(days=days)
            deadline = time.monotonic() + MEDIA_CAP_SECONDS
            last_id = 0   # ids ascend, so a file that failed or was kept in this run is never selected again
            while time.monotonic() < deadline:
                batch = _candidates(cutoff, last_id)
                if not batch:
                    break
                last_id = batch[-1].id
                for file in batch:
                    if not _has_an_outside_source(file) or _local_owners([file.id]):
                        continue
                    file_id = file.id
                    try:
                        with session.begin_nested():
                            file.evict_local_copies(cache_urls)
                        evicted += 1
                    except Exception as e:
                        failed += 1
                        current_app.logger.warning(f'media cap: could not evict File {file_id}: {type(e).__name__}')
                session.commit()
            if cache_urls:
                flush_cdn_cache(cache_urls)
        current_app.logger.info(f'media cap: evicted {evicted} files, failed {failed}')
        return {'evicted': evicted, 'failed': failed}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def remove_user_orphans(root: str = os.path.join(MEDIA_ROOT, 'users')) -> int:
    """Remove files under `root` that no File names as its `file_path` or `thumbnail_path`, compared as written. The
    names are read once: a query per file took about an hour for 151K files in production (2026-11-07). A file younger
    than ORPHAN_MIN_AGE_SECONDS stays, as its File may be newer than the names read here."""
    named = set()
    for file_path, thumbnail_path in db.session.query(File.file_path, File.thumbnail_path).yield_per(10000):
        named.update(value for value in (file_path, thumbnail_path) if value)

    removed = 0
    too_new = time.time() - ORPHAN_MIN_AGE_SECONDS
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if path in named:
                continue
            try:
                if os.path.getmtime(path) > too_new:
                    continue
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                continue
    current_app.logger.info(f'orphan sweep: removed {removed} files under {root}')
    return removed


def remove_post_orphans() -> int:
    """Remove files in `media/posts` that no record names: what deleted posts left, as `delete_from_disk` keeps a file
    its row cannot prove we wrote. The set of known names only saves a query per file that is plainly in use; a file
    outside it is judged by `named_by_any_other_record`, which keeps it on any doubt."""
    known = set()
    rows = db.session.query(File.file_path, File.thumbnail_path).yield_per(10000)
    for file_path, thumbnail_path in rows:
        known.update(os.path.basename(value) for value in (file_path, thumbnail_path) if value)

    removed = 0
    deadline = time.monotonic() + ORPHAN_SWEEP_SECONDS
    too_new = time.time() - ORPHAN_MIN_AGE_SECONDS
    for root, _dirs, names in os.walk(os.path.join(MEDIA_ROOT, 'posts')):
        for name in names:
            if time.monotonic() >= deadline:
                current_app.logger.info(f'media cap: orphan sweep out of time, removed {removed}')
                return removed
            if name in known or os.path.splitext(name)[1].lower() not in ORPHAN_EXTENSIONS:
                continue
            path = os.path.realpath(os.path.join(root, name))
            try:
                if os.path.getmtime(path) > too_new or named_by_any_other_record(path, 0):
                    continue
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                continue
    current_app.logger.info(f'media cap: orphan sweep removed {removed}')
    return removed
