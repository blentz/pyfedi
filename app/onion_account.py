"""Stored IPs of onion-registered accounts (onion unlinkability spec 2026-10-26, goals 2 and 2a).

An account whose home domain is the onion always stores `ONION_ACCOUNT_IP` and no country, whatever it does and over
whichever domain, so nothing the server keeps ties its IP to onion use. Any other account keeps what it has when it acts
over the onion.

This is not in app/onion.py because that module is imported before `app.storefront` can be.
"""
from __future__ import annotations

from app.onion import ONION_ACCOUNT_IP, audit_field
from app.storefront import domain_by_id


def is_onion_account(user) -> bool:
    """Whether `user` registered on an onion domain. `site_domain_id` must already be set."""
    return domain_by_id(user.site_domain_id).is_onion


def audit_ip(user, value: str | None) -> str | object:
    """`user.ip_address` to store: the sentinel for an onion account, `KEEP` for an onion request by any other
    account, `value` otherwise."""
    if is_onion_account(user):
        return ONION_ACCOUNT_IP
    return audit_field(value)


def audit_country(user, value) -> str | object:
    """`user.ip_address_country` to store: None for an onion account, `KEEP` for an onion request by any other
    account, `value` otherwise."""
    if is_onion_account(user):
        return None
    return audit_field(value)
