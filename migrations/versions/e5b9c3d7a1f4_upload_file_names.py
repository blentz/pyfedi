"""an upload recorded before file names were kept gets the name the upload gave its file (plan C3)

Revision ID: e5b9c3d7a1f4
Revises: d3a7e5c1f9b2
Create Date: 2026-10-24 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5b9c3d7a1f4'
down_revision = 'd3a7e5c1f9b2'
branch_labels = None
depends_on = None

# A File's `file_name` is what lets its record remove the local file its `source_url` names (File.wrote_its_source):
# only our upload code sets it. A media-library record made before that is proven ours by its user_file size. Only
# two places have ever written user_file (git history, 23f359cd5 to today): `process_upload`, which records the
# stored file's os.path.getsize (from f627ef1f7, the commit that added the column), and the media library's URL box
# (bc7bf012a), which records 0 for a URL anyone typed. So a size above 0 is an upload's, and its name is the last
# segment of the URL the upload built (`<15 random characters><extension>`). A record already named keeps its name.
BACKFILL = '''
    UPDATE file SET file_name = regexp_replace(source_url, '^.*/', '')
    WHERE file_name IS NULL AND source_url IS NOT NULL
      AND id IN (SELECT file_id FROM user_file WHERE size > 0)
'''


def backfill_file_names(connection) -> None:
    connection.execute(sa.text(BACKFILL))


def upgrade():
    backfill_file_names(op.get_bind())


def downgrade():
    # Nothing to undo that can be told apart: the names given are the ones the uploads gave their files.
    pass
