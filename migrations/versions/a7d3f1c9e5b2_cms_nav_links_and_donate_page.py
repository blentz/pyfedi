"""CMS pages can be linked from the header and the footer, and /donate becomes a seeded CMS page

Revision ID: a7d3f1c9e5b2
Revises: e4c8a1d7f3b6
Create Date: 2026-11-05 20:00:00.000000

Spec docs/superpowers/specs/2026-11-05-cms-donate-page-design.md. The text is frozen here: a migration must not change
with the app. A site that already has a /donate page keeps its words and gets the links and the sidebar it had until now.

The downgrade removes the page only while it is word for word the seed: an admin's edit is not ours to throw away.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d3f1c9e5b2'
down_revision = 'e4c8a1d7f3b6'
branch_labels = None
depends_on = None

URL = '/donate'
TITLE = 'Donate'
BODY = """PieFed is free and open-source software while operating without any advertising, monetization, or reliance on venture capital. Your donations are vital in supporting the PieFed development effort, allowing us to expand and enhance PieFed with new features.

<div class="btn-group btn-group-lg"><a class="btn btn-primary" href="https://www.patreon.com/PieFed">Donate using Patreon</a><a class="btn btn-primary" href="https://liberapay.com/PieFed/">Donate using Liberapay</a><a class="btn btn-primary" href="https://ko-fi.com/piefed">Donate using Ko-fi</a></div>

To support this site directly, see the [donation plans](/donations).
"""
BODY_HTML = """<p>PieFed is free and open-source software while operating without any advertising, monetization, or reliance on venture capital. Your donations are vital in supporting the PieFed development effort, allowing us to expand and enhance PieFed with new features.</p>
<div class="btn-group btn-group-lg"><a class="btn btn-primary" href="https://www.patreon.com/PieFed" rel="nofollow ugc" target="">Donate using Patreon</a><a class="btn btn-primary" href="https://liberapay.com/PieFed/" rel="nofollow ugc" target="">Donate using Liberapay</a><a class="btn btn-primary" href="https://ko-fi.com/piefed" rel="nofollow ugc" target="">Donate using Ko-fi</a></div>
<p>To support this site directly, see the <a href="/donations" rel="nofollow ugc" target="">donation plans</a>.</p>
"""


def upgrade():
    with op.batch_alter_table('cms_page', schema=None) as batch_op:
        batch_op.add_column(sa.Column('in_header', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('in_footer', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('show_sidebar', sa.Boolean(), server_default=sa.false(), nullable=False))

    conn = op.get_bind()
    conn.execute(sa.text('UPDATE cms_page SET in_header = true, in_footer = true, show_sidebar = true WHERE url = :url'),
                 {'url': URL})
    conn.execute(sa.text("""
        INSERT INTO cms_page (url, title, body, body_html, last_edited_by, created_at, edited_at, in_header, in_footer,
                              show_sidebar)
        SELECT :url, :title, :body, :body_html, 'PieFed', now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc', true, true,
               true
        WHERE NOT EXISTS (SELECT 1 FROM cms_page WHERE url = :url)
    """), {'url': URL, 'title': TITLE, 'body': BODY, 'body_html': BODY_HTML})


def downgrade():
    op.get_bind().execute(sa.text('DELETE FROM cms_page WHERE url = :url AND body = :body'), {'url': URL, 'body': BODY})
    with op.batch_alter_table('cms_page', schema=None) as batch_op:
        batch_op.drop_column('show_sidebar')
        batch_op.drop_column('in_footer')
        batch_op.drop_column('in_header')
