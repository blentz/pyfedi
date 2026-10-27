"""notification.local_only: whether a notification may name a local-only domain's row (follow-up F6, round 5)

Off its own domain a local-only domain's rows are hidden, and so is a notification that names one
(app.hidden_routes.hidden_notifications). That rule reads each notification's link with the URL map, so a count of the
shown notifications had to read every notification. The stamp lets a count read only the notifications that may name
such a row: a notification without it names none, on any storefront.

`notification_names_local_only(notification, default_host)` is the one rule, for the backfill below and for every new
notification (app.models stamps them as they are flushed). It is true when the notification's author, a row its
`targets` name, or a row or host its link names is local-only, and for every link it cannot read (a name, a page it
does not know): those are left to the per-link rule. On a site where every domain federates it is false at once.

Revision ID: c6e2a8f4b1d9
Revises: a9c4e6b2d8f1
Create Date: 2026-10-27 09:00:00.000000

"""
from alembic import op
from flask import current_app
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c6e2a8f4b1d9'
down_revision = 'a9c4e6b2d8f1'
branch_labels = None
depends_on = None

FUNCTION = r"""
CREATE OR REPLACE FUNCTION notification_names_local_only(n notification, default_host text) RETURNS boolean
LANGUAGE plpgsql STABLE AS $function$
DECLARE
    default_closed boolean;
    link text := coalesce(n.url, '');
    host text;
    parts text[];
    target text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM site_domain WHERE NOT federates) THEN
        RETURN false;   -- no local-only domain: no local-only row
    END IF;
    default_closed := EXISTS (SELECT 1 FROM site_domain WHERE is_default AND NOT federates);

    -- an account or community is local-only by its home domain; one with no known domain is the default domain's
    IF notification_local_only_user(n.author_id::bigint, default_closed) THEN
        RETURN true;
    END IF;

    IF n.targets IS NOT NULL AND json_typeof(n.targets) = 'object' THEN
        FOREACH target IN ARRAY ARRAY['post_id', 'suspect_post_id'] LOOP
            IF EXISTS (SELECT 1 FROM post WHERE id = notification_target_id(n.targets, target) AND local_only) THEN
                RETURN true;
            END IF;
        END LOOP;
        FOREACH target IN ARRAY ARRAY['comment_id', 'suspect_comment_id'] LOOP
            IF EXISTS (SELECT 1 FROM post_reply WHERE id = notification_target_id(n.targets, target)
                       AND local_only) THEN
                RETURN true;
            END IF;
        END LOOP;
        IF notification_local_only_user(notification_target_id(n.targets, 'author_id'), default_closed)
                OR notification_local_only_user(notification_target_id(n.targets, 'suspect_user_id'), default_closed)
                OR notification_local_only_community(notification_target_id(n.targets, 'community_id'),
                                                     default_closed) THEN
            RETURN true;
        END IF;
    END IF;

    -- the link: an absolute one on a local-only domain names its row; on another site, nothing of ours
    IF link ~* '^[a-z][a-z0-9+.-]*://' THEN
        host := lower(substring(link FROM '^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)'));
        IF EXISTS (SELECT 1 FROM site_domain WHERE NOT federates AND position(lower(domain) IN host) > 0) THEN
            RETURN true;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM site_domain WHERE position(lower(domain) IN host) > 0)
                AND NOT (coalesce(default_host, '') <> '' AND position(lower(default_host) IN host) > 0) THEN
            RETURN false;   -- another site's page names none of our rows
        END IF;
        link := substring(link FROM '^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*(.*)$');
    END IF;
    link := regexp_replace(link, '[?#].*$', '');

    IF link IN ('', '/', '/admin/reports', '/notifications', '/user/follow_requests') THEN
        RETURN false;
    END IF;
    parts := regexp_match(link, '^/post/([0-9]{1,18})(?:/comment/([0-9]{1,18}))?$');
    IF parts IS NOT NULL THEN
        RETURN EXISTS (SELECT 1 FROM post WHERE id = parts[1]::bigint AND local_only)
            OR (parts[2] IS NOT NULL AND EXISTS (SELECT 1 FROM post_reply WHERE id = parts[2]::bigint AND local_only));
    END IF;
    parts := regexp_match(link, '^/comment/([0-9]{1,18})$');
    IF parts IS NOT NULL THEN
        RETURN EXISTS (SELECT 1 FROM post_reply WHERE id = parts[1]::bigint AND local_only);
    END IF;
    parts := regexp_match(link, '^/user/([0-9]{1,18})$');
    IF parts IS NOT NULL THEN
        RETURN notification_local_only_user(parts[1]::bigint, default_closed);
    END IF;
    parts := regexp_match(link, '^/community/([0-9]{1,18})$');
    IF parts IS NOT NULL THEN
        RETURN notification_local_only_community(parts[1]::bigint, default_closed);
    END IF;
    parts := regexp_match(link, '^/chat/([0-9]{1,18})$');
    IF parts IS NOT NULL THEN
        RETURN EXISTS (SELECT 1 FROM conversation_member m WHERE m.conversation_id = parts[1]::bigint
                       AND notification_local_only_user(m.user_id::bigint, default_closed));
    END IF;
    parts := regexp_match(link, '^/chat/ban_from_mod/([0-9]{1,18})/([0-9]{1,18})$');
    IF parts IS NOT NULL THEN
        RETURN notification_local_only_user(parts[1]::bigint, default_closed)
            OR notification_local_only_community(parts[2]::bigint, default_closed);
    END IF;
    RETURN true;   -- a link this rule cannot read (a name, another page): the per-link rule decides
END
$function$;
"""

ACTORS = r"""
CREATE OR REPLACE FUNCTION notification_target_id(targets json, key text) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $function$
    SELECT CASE WHEN json_typeof(targets -> key) = 'number' AND (targets ->> key) ~ '^[0-9]{1,18}$'
                THEN (targets ->> key)::bigint END
$function$;

CREATE OR REPLACE FUNCTION notification_local_only_user(user_id bigint, default_closed boolean) RETURNS boolean
LANGUAGE sql STABLE AS $function$
    SELECT EXISTS (SELECT 1 FROM "user" u WHERE u.id = user_id AND u.ap_id IS NULL AND (
        EXISTS (SELECT 1 FROM site_domain d WHERE d.id = u.site_domain_id AND NOT d.federates)
        OR (default_closed AND NOT EXISTS (SELECT 1 FROM site_domain d WHERE d.id = u.site_domain_id))))
$function$;

CREATE OR REPLACE FUNCTION notification_local_only_community(community_id bigint, default_closed boolean)
RETURNS boolean LANGUAGE sql STABLE AS $function$
    SELECT EXISTS (SELECT 1 FROM community c WHERE c.id = community_id AND c.ap_id IS NULL AND (
        EXISTS (SELECT 1 FROM site_domain d WHERE d.id = c.site_domain_id AND NOT d.federates)
        OR (default_closed AND NOT EXISTS (SELECT 1 FROM site_domain d WHERE d.id = c.site_domain_id))))
$function$;
"""

# Existing notifications: only those the rule stamps are written, so a site where every domain federates writes none.
# `default_host` is the default domain's host, which has no site_domain row on a site that never added one.
BACKFILL = ('UPDATE notification SET local_only = true '
            'WHERE notification_names_local_only(notification, :default_host)')


def default_host() -> str:
    return (current_app.config.get('SERVER_NAME') or '').split(':')[0].strip('.').lower()


def upgrade():
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_only', sa.Boolean(), server_default='false', nullable=False))
    op.execute(ACTORS)
    op.execute(FUNCTION)
    op.get_bind().execute(sa.text(BACKFILL), {'default_host': default_host()})
    op.execute('CREATE INDEX ix_notification_user_local_only ON notification (user_id) WHERE local_only')


def downgrade():
    op.execute('DROP INDEX IF EXISTS ix_notification_user_local_only')
    op.execute('DROP FUNCTION IF EXISTS notification_names_local_only(notification, text)')
    op.execute('DROP FUNCTION IF EXISTS notification_local_only_user(bigint, boolean)')
    op.execute('DROP FUNCTION IF EXISTS notification_local_only_community(bigint, boolean)')
    op.execute('DROP FUNCTION IF EXISTS notification_target_id(json, text)')
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_column('local_only')
