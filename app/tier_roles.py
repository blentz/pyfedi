"""Roles carried by subscription tiers (plan docs/superpowers/plans/2026-11-16-tier-roles.md).

A member holds the roles of the tiers they subscribe to while they have access (T2): a grant marked `via_tier`, made
here and ended here. It ends by its expiry: when a tier no longer gives the role, the grant expires when the member's
access does (the lapse grace, where the account lapses on its domain; else at once). The checks ignore an expired grant
and app.roles.remove_expired_grants deletes it. A role an admin granted is never touched here (D2).

Only the models are needed, so that app.complimentary (which app.utils imports) and app.payments.core can give roles.
The caches that hold role names belong to the API's views, which import far more: `role_names_changed` tells them,
and app.roles listens.
"""
from datetime import timedelta

from blinker import Namespace
from sqlalchemy import select

from app import db
from app.models import MemberSubscription, ModLog, Offer, Role, RoleGrant, User, offer_role, utcnow
from app.permission_cache import permissions_changed
from app.permissions import home_domain_id
from app.storefront import domain_by_id, paid_registration

# Sent when a member's roles changed here, so whatever caches role names (badges, `is_admin`) empties itself (RB15)
role_names_changed = Namespace().signal('role-names-changed')


def offer_roles(offer: Offer) -> list[Role]:
    """The roles `offer` carries, by name."""
    return Role.query.join(offer_role, offer_role.c.role_id == Role.id) \
        .filter(offer_role.c.offer_id == offer.id).order_by(Role.name).all()


def _wanted(user: User) -> dict[int, str]:
    """Each role the member's active subscriptions give, with the title of a tier that gives it (D3)."""
    rows = db.session.execute(
        select(offer_role.c.role_id, Offer.title).select_from(MemberSubscription)
        .join(Offer, Offer.id == MemberSubscription.offer_id)
        .join(offer_role, offer_role.c.offer_id == Offer.id)
        .where(MemberSubscription.user_id == user.id, MemberSubscription.status == 'active')
        .order_by(Offer.id)).all()
    wanted = {}
    for role_id, title in rows:
        wanted.setdefault(role_id, title)
    return wanted


def access_ends(user: User):
    """When the member's access ends (T2): the lapse grace after their subscription ended, where their domain charges
    now; otherwise, and once that has passed, now. The domain is asked again: a tier's roles changing, or a grant
    running out, syncs members without refresh_access, and a lapse kept from when the domain charged gives no grace."""
    now = utcnow()
    if user.access_lapsed_at is None:
        return now
    home = domain_by_id(user.site_domain_id)
    if not paid_registration(home):
        return now
    return max(user.access_lapsed_at + timedelta(days=home.lapse_grace_days), now)


def sync_tier_roles(user: User) -> bool:
    """Grant the roles the member's tiers give, and set the end of those they no longer give (D3). Whether anything
    changed; the caller commits. A member's permissions are re-read on their next request."""
    wanted = _wanted(user)
    grants = {grant.role_id: grant for grant in RoleGrant.query.filter_by(user_id=user.id)}
    changed = False
    for role_id, title in wanted.items():
        grant = grants.get(role_id)
        if grant is None:
            db.session.add(RoleGrant(user_id=user.id, role_id=role_id, via_tier=True, granted_at=utcnow()))
            role = db.session.get(Role, role_id)
            db.session.add(ModLog(type='admin', action='grant_role', target_user_id=user.id,
                                  reason=f'{role.name} (through the subscription {title})', link=f'u/{user.link()}',
                                  link_text=user.display_name(), public=False, site_domain_id=home_domain_id(user)))
            changed = True
        elif grant.via_tier and grant.expires_at is not None:
            grant.expires_at = None
            changed = True
    ends = None
    for grant in grants.values():
        if grant.via_tier and grant.role_id not in wanted and grant.expires_at is None:
            ends = ends or access_ends(user)
            grant.expires_at = ends
            changed = True
    if changed:
        db.session.flush()
        permissions_changed(user.id)
        role_names_changed.send(user)
    return changed
