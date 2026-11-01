"""album posts become gallery posts

Revision ID: c3e9a7d1f5b2
Revises: b8d1e6f4a2c7
Create Date: 2026-11-01 12:00:00.000000

Spec 2026-11-01 G-8: an image post with an album (type 3, gallery_count > 0: the image, then the other pictures at
post_file weights 1..) becomes a gallery post (type 7) whose gallery is every picture. A copy of the image's File is
inserted at weight 0 -- url, alt text, size, hash and its paths, flagged as not written by us, so deleting the copy
never removes the image's files -- and gallery_count counts it. The image File stays as the thumbnail, and post.url
keeps the first picture.

The downgrade reverses exactly the posts this converted: gallery posts whose weight-0 picture is a copy of their
image (same source_url, written by us false). Galleries made after the upgrade have a composite thumbnail and are
left alone.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3e9a7d1f5b2'
down_revision = 'b8d1e6f4a2c7'
branch_labels = None
depends_on = None

POST_TYPE_IMAGE = 3
POST_TYPE_GALLERY = 7


def upgrade():
    bind = op.get_bind()
    posts = bind.execute(sa.text(
        'SELECT id, image_id FROM "post" WHERE type = :image AND gallery_count > 0 AND image_id IS NOT NULL'),
        {'image': POST_TYPE_IMAGE}).all()
    for post_id, image_id in posts:
        copy_id = bind.execute(sa.text(
            'INSERT INTO "file" (source_url, alt_text, width, height, hash, file_path, thumbnail_path, '
            'thumbnail_width, thumbnail_height, file_written_by_us, thumbnail_written_by_us) '
            'SELECT source_url, alt_text, width, height, hash, file_path, thumbnail_path, thumbnail_width, '
            'thumbnail_height, false, false FROM "file" WHERE id = :image_id RETURNING id'),
            {'image_id': image_id}).scalar()
        bind.execute(sa.text('INSERT INTO "post_file" (post_id, file_id, weight) VALUES (:post_id, :file_id, 0)'),
                     {'post_id': post_id, 'file_id': copy_id})
        bind.execute(sa.text('UPDATE "post" SET type = :gallery, gallery_count = gallery_count + 1 WHERE id = :id'),
                     {'gallery': POST_TYPE_GALLERY, 'id': post_id})


def downgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        'SELECT p.id, pf.file_id FROM "post" p JOIN "post_file" pf ON pf.post_id = p.id AND pf.weight = 0 '
        'JOIN "file" copy ON copy.id = pf.file_id JOIN "file" image ON image.id = p.image_id '
        'WHERE p.type = :gallery AND copy.source_url = image.source_url AND copy.file_written_by_us IS false'),
        {'gallery': POST_TYPE_GALLERY}).all()
    for post_id, file_id in rows:
        bind.execute(sa.text('DELETE FROM "post_file" WHERE post_id = :post_id AND file_id = :file_id'),
                     {'post_id': post_id, 'file_id': file_id})
        bind.execute(sa.text('DELETE FROM "file" WHERE id = :file_id'), {'file_id': file_id})
        bind.execute(sa.text('UPDATE "post" SET type = :image, gallery_count = gallery_count - 1 WHERE id = :id'),
                     {'image': POST_TYPE_IMAGE, 'id': post_id})
