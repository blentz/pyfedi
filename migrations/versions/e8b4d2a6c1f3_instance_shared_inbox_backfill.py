"""deliver to the shared inbox an instance's actors name, not the /inbox guess

Every follower fan-out posts to `instance.inbox`, and find_instance_id guessed `https://<host>/inbox` for it, kept
unless the site root answered as an Application actor. A peer whose shared inbox is elsewhere got none of it: Pixelfed's
is /f/inbox, so every Create, Update and Delete to a Pixelfed follower was a 405 (P1, Pixelfed spike 2026-10-28). New
actors now teach their instance the shared inbox they name; this does the same for the actors already stored.

An actor's stored `ap_inbox_url` is its shared inbox when it has one, else its own inbox, so a stored URL counts as the
instance's shared inbox when two or more of its actors share it, or when an actor's own inbox is known and differs.
Only a URL on the instance's own host is taken, and hosted domains' rows are left alone. Where an instance has more
than one such URL, the most used wins.

Data only: the downgrade leaves the learned inboxes in place, since the guesses they replace were wrong.

Revision ID: e8b4d2a6c1f3
Revises: d5a9c3e7f1b4
Create Date: 2026-10-28 23:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'e8b4d2a6c1f3'
down_revision = 'd5a9c3e7f1b4'
branch_labels = None
depends_on = None

BACKFILL_SHARED_INBOXES = '''
WITH actor AS (
    SELECT instance_id, ap_inbox_url AS inbox,
           (ap_own_inbox_url IS NOT NULL AND ap_own_inbox_url <> ap_inbox_url) AS own_inbox_differs
      FROM "user" WHERE ap_id IS NOT NULL AND instance_id IS NOT NULL AND ap_inbox_url <> ''
    UNION ALL
    SELECT instance_id, ap_inbox_url, false
      FROM community WHERE ap_id IS NOT NULL AND instance_id IS NOT NULL AND ap_inbox_url <> ''
    UNION ALL
    SELECT instance_id, ap_inbox_url, false
      FROM feed WHERE ap_id IS NOT NULL AND instance_id IS NOT NULL AND ap_inbox_url <> ''
),
candidate AS (
    SELECT actor.instance_id, actor.inbox, count(*) AS uses
      FROM actor JOIN instance ON instance.id = actor.instance_id
     WHERE lower(split_part(split_part(actor.inbox, '://', 2), '/', 1)) = instance.domain
     GROUP BY actor.instance_id, actor.inbox
    HAVING count(*) >= 2 OR bool_or(actor.own_inbox_differs)
),
best AS (
    SELECT DISTINCT ON (instance_id) instance_id, inbox FROM candidate ORDER BY instance_id, uses DESC, inbox
)
UPDATE instance SET inbox = best.inbox, shared_inbox = best.inbox
  FROM best
 WHERE instance.id = best.instance_id
   AND instance.id NOT IN (SELECT instance_id FROM site_domain WHERE instance_id IS NOT NULL)
   AND (instance.inbox IS DISTINCT FROM best.inbox OR instance.shared_inbox IS DISTINCT FROM best.inbox)
'''


def upgrade():
    op.execute(BACKFILL_SHARED_INBOXES)


def downgrade():
    pass
