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
from app.models import PromoCode, PromoCodeUse, Referral, User, utcnow
from app.payments.gifts import referral_benefit, referral_gift_code, start_gift, usable
from app.storefront import REFERRAL_MODE, DomainInfo, domain_base_url, open_registrations, paid_registration  # noqa: F401 (re-exported)
from app.utils import get_setting

from app.permissions import user_access
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
    """Give `user` a fresh code, replacing (and so revoking) any earlier one, its gift kept (paid membership G2). The
    caller commits."""
    user.referral_nonce = secrets.token_hex(16)
    user.referral_code = derive_code(user.user_name, user.referral_nonce)
    referral_gift_code(user)
    return user.referral_code


def may_generate(user: User) -> bool:
    """RC8: a local, verified, not banned or deleted account that the 'Who can generate' setting allows."""
    if user is None or user.ap_id is not None or user.banned or user.deleted or not user.verified:
        return False
    who = get_setting(GENERATORS_SETTING, DEFAULT_GENERATORS)
    if who == 'all':
        return True
    if who == 'staff':
        return user_access('generate referral codes', user.id)
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
    if user_access('exempt from account limits', user.id):
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
    """Add the who-referred-whom row for `referred`, made with `referrer`'s current code, and where `referred`'s domain
    charges, the gift that code gives (paid membership PM3, G2, G4; plan 2026-11-16 R1, R4). A code that gives a
    discount gave it at registration, held for the first payment. The caller commits."""
    row = Referral(referrer_id=referrer.id, referred=referred, code=referrer.referral_code, created_at=utcnow())
    db.session.add(row)
    code = referral_gift_code(referrer)
    if code is None or not usable(code) or not paid_registration(referred.home_domain()):
        return row
    benefit = referral_benefit(code, referrer.home_domain())
    if benefit is not None and benefit.offer is not None:
        db.session.flush()   # the gift's subscription names the new account
        start_gift(referred, code, commit=False, offer=benefit.offer, days=benefit.days)
    elif benefit is not None and benefit.discount is not None:   # a use of the referral code, as a gift is (R5)
        db.session.flush()
        db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=referred.id, used_at=utcnow()))
    return row


def referral_discount(referrer: User) -> PromoCode | None:
    """The discount code `referrer`'s referral code gives, if it gives one (R1, R4). The discount's own limits are the
    caller's to ask, since one past them refuses the registration rather than admitting for free (R5). The referral
    code's own (its switch, last day and uses) are asked here: past them it gives no discount, as it would give no
    gift, and the caller refuses. Under referrer_for's lock on the referrer, so its uses can't be overtaken."""
    code = referral_gift_code(referrer)
    benefit = referral_benefit(code, referrer.home_domain()) if code is not None else None
    return benefit.discount if benefit is not None else None


def referral_discount_refused(referrer: User) -> bool:
    """Whether `referrer`'s referral code gives a discount but is past its own limits (security review 2026-11-16)."""
    code = referral_gift_code(referrer)
    return code is not None and referral_discount(referrer) is not None and not usable(code)


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
