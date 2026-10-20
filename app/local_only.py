"""Local-only content: the accounts and communities of a hosted domain whose `federates` setting is off.

They never leave this server (spec docs/superpowers/specs/2026-10-19-per-domain-identity-design.md §3.4).
Because that is decided per domain, a domain's `federates` setting is locked once it owns accounts or communities.
"""
from sqlalchemy import text

from app import db

# A domain owns the local rows stamped with its id. The default domain also owns the local rows no domain was
# stamped on (legacy rows); remote rows carry an ap_id and never count.
_OWNERS = text("""
    SELECT EXISTS (
        SELECT 1 FROM "user" u
        WHERE u.site_domain_id = :id
           OR (u.site_domain_id IS NULL AND u.ap_id IS NULL
               AND EXISTS (SELECT 1 FROM site_domain d WHERE d.id = :id AND d.is_default))
    ) OR EXISTS (
        SELECT 1 FROM community c
        WHERE c.site_domain_id = :id
           OR (c.site_domain_id IS NULL AND c.ap_id IS NULL
               AND EXISTS (SELECT 1 FROM site_domain d WHERE d.id = :id AND d.is_default))
    )
""")


def domain_has_owners(site_domain_id: int) -> bool:
    """True when the domain owns at least one local account or community."""
    with db.session.no_autoflush:
        return bool(db.session.execute(_OWNERS, {'id': site_domain_id}).scalar())


def user_local_only(user) -> bool:
    """True for a local account whose home domain does not federate. A remote account never is."""
    return user.ap_id is None and not user.home_domain().federates


def community_local_only(community) -> bool:
    """True for a local community whose home domain does not federate. A remote community never is."""
    return community.ap_id is None and not community.home_domain().federates


def content_local_only(author, community, parent=None) -> bool:
    """Whether what `author` makes in `community` (answering `parent`, a post or a reply) stays on this server.

    Any of the three being local-only is enough; a remote author's content is never stamped.
    """
    if author.ap_id is not None:
        return False
    return user_local_only(author) or community_local_only(community) or bool(parent is not None and parent.local_only)
