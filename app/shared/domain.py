from flask import abort, flash
from flask_babel import _
from flask_login import current_user

from app import db, cache
from app.constants import *
from app.models import DomainBlock, Domain
from app.storefront import is_local_host
from app.utils import authorise_api_user, blocked_domains, canonical_host


def block_domain(domain, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    # canonical_host: Domain names hold an IPv6 literal in one spelling, and every spelling is one address
    domain_to_block = db.session.query(Domain).filter(Domain.name == canonical_host(domain)).first()

    # D581: an unknown domain is an error, not a silent success, and nobody blocks this instance's own domain
    if domain_to_block is None:
        if src == SRC_API:
            raise Exception('domain_not_found')
        abort(404)
    if is_local_host(domain_to_block.name):
        if src == SRC_API:
            raise Exception('cannot_block_own_domain')
        flash(_('You cannot block this instance\'s own domain.'), 'error')
        return

    existing_block = db.session.query(DomainBlock).filter(DomainBlock.domain_id == domain_to_block.id, DomainBlock.user_id == user_id).first()
    if not existing_block:
        block = DomainBlock(domain_id=domain_to_block.id, user_id=user_id)
        db.session.add(block)
        db.session.commit()

        cache.delete_memoized(blocked_domains, user_id)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect


def unblock_domain(domain, src, auth=None):
    if src == SRC_API:
        user_id = authorise_api_user(auth)
    else:
        user_id = current_user.id

    domain_to_unblock = db.session.query(Domain).filter(Domain.name == canonical_host(domain)).first()

    if domain_to_unblock is None:  # D581
        if src == SRC_API:
            raise Exception('domain_not_found')
        abort(404)

    existing_block = db.session.query(DomainBlock).filter(DomainBlock.domain_id == domain_to_unblock.id, DomainBlock.user_id == user_id).first()
    if existing_block:
        db.session.delete(existing_block)
        db.session.commit()

        cache.delete_memoized(blocked_domains, user_id)

    if src == SRC_API:
        return user_id
    else:
        return  # let calling function handle confirmation flash message and redirect