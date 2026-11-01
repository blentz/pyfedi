"""Referral-code registration (spec docs/superpowers/specs/2026-10-31-referral-codes-design.md).

While `Site.registration_mode` is 'ReferralCode', an account is created only from a signup carrying a valid code,
and is approved without the application queue. A user's code is `uuid5(NS, f"{user_name}:{nonce}")` with
`NS = uuid5(NAMESPACE_DNS, SERVER_NAME)`: the random per-user nonce is what makes it unguessable (a bare
`uuid5(NS, user_name)` can be computed by anyone), and regenerating replaces the nonce, which revokes the old code.
"""
import io
import secrets
import uuid

import segno
from flask import current_app, g, url_for
from sqlalchemy import func

from app import db
from app.models import Referral, User, utcnow
from app.storefront import REFERRAL_MODE, DomainInfo, domain_base_url, open_registrations  # noqa: F401 (re-exported)
from app.utils import get_setting

GENERATORS_SETTING = 'referral_generators'
MAX_PER_CODE_SETTING = 'referral_max_per_code'
MAX_PER_USER_SETTING = 'referral_max_per_user'
GENERATOR_CHOICES = ('admins', 'staff', 'all')
DEFAULT_GENERATORS = 'admins'
DEFAULT_PROMPT = 'Enter your referral code'


def referral_mode_on(site) -> bool:
    return site is not None and site.registration_mode == REFERRAL_MODE


def referral_namespace() -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, current_app.config['SERVER_NAME'])


def derive_code(user_name: str, nonce: str) -> str:
    return str(uuid.uuid5(referral_namespace(), f'{user_name}:{nonce}'))


def generate_code(user: User) -> str:
    """Give `user` a fresh code, replacing (and so revoking) any earlier one. The caller commits."""
    user.referral_nonce = secrets.token_hex(16)
    user.referral_code = derive_code(user.user_name, user.referral_nonce)
    return user.referral_code


def may_generate(user: User) -> bool:
    """RC8: a local, verified, not banned or deleted account that the 'Who can generate' setting allows."""
    if user is None or user.ap_id is not None or user.banned or user.deleted or not user.verified:
        return False
    who = get_setting(GENERATORS_SETTING, DEFAULT_GENERATORS)
    if who == 'all':
        return True
    if who == 'staff':
        return user.is_admin_or_staff()
    return user.is_admin()


def uses_of_current_code(user: User) -> int:
    if not user.referral_code:
        return 0
    return db.session.query(func.count(Referral.id)).filter(Referral.referrer_id == user.id,
                                                            Referral.code == user.referral_code).scalar()


def lifetime_referrals(user: User) -> int:
    return db.session.query(func.count(Referral.id)).filter(Referral.referrer_id == user.id).scalar()


def caps() -> tuple[int, int]:
    """(max signups per code, max referrals per user); 0 is unlimited."""
    return int(get_setting(MAX_PER_CODE_SETTING, 0) or 0), int(get_setting(MAX_PER_USER_SETTING, 0) or 0)


def remaining_signups(user: User) -> tuple[int | None, int | None]:
    """(signups left on the current code, signups left in total); None is unlimited. The code can never take more than
    the lifetime cap has left, so its figure is the lower of the two. Admins are exempt (RC4)."""
    if user.is_admin():
        return None, None
    per_code, per_user = caps()
    lifetime_left = max(per_user - lifetime_referrals(user), 0) if per_user else None
    code_left = max(per_code - uses_of_current_code(user), 0) if per_code else None
    if lifetime_left is not None:
        code_left = lifetime_left if code_left is None else min(code_left, lifetime_left)
    return code_left, lifetime_left


def under_caps(user: User) -> bool:
    """Whether the current code can take another signup: the same figure the settings page shows."""
    return remaining_signups(user)[0] != 0


def normalise_code(text: str | None) -> str | None:
    try:
        return str(uuid.UUID((text or '').strip()))
    except ValueError:
        return None


def referrer_for(text: str | None, storefront: DomainInfo) -> User | None:
    """The account whose current code `text` is, if that code may be used to sign up on `storefront` now; else None.

    The referrer's row is locked until the caller's commit, so two signups at once cannot both slip under a cap."""
    code = normalise_code(text)
    if code is None:
        return None
    user = User.query.filter(User.referral_code == code).with_for_update(of=User).first()
    if user is None or not may_generate(user):
        return None
    if user.home_domain().id != storefront.id:   # RC10: a code works on its referrer's home storefront only
        return None
    return user if under_caps(user) else None


def record_referral(referrer: User, referred: User) -> Referral:
    """Add the who-referred-whom row for `referred`, made with `referrer`'s current code. The caller commits."""
    row = Referral(referrer_id=referrer.id, referred=referred, code=referrer.referral_code, created_at=utcnow())
    db.session.add(row)
    return row


def referral_link(user: User) -> str:
    """RC9: the registration URL on the referrer's own home storefront."""
    return domain_base_url(user.home_domain()) + url_for('auth.register', ref=user.referral_code)


def referral_qr_svg(link: str) -> str:
    return segno.make(link, error='m').svg_inline(scale=5, title='QR code', border=2, dark='#000', light='#fff')


def referral_qr_png(link: str) -> bytes:
    out = io.BytesIO()
    segno.make(link, error='m').save(out, kind='png', scale=8, border=2)
    return out.getvalue()


def referrals_available(user) -> bool:
    """Whether `user` gets the referral-code settings page: referral mode is on and they may generate codes."""
    return getattr(user, 'is_authenticated', False) and referral_mode_on(g.site) and may_generate(user)
