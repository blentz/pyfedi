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
from app.payments.gifts import GiftUnavailable, default_referral_benefit, referral_benefit, referral_gift_code, \
    start_gift, tier_sold_on, usable
from app.storefront import REFERRAL_MODE, DomainInfo, current_storefront, domain_base_url, open_registrations, \
    paid_registration, signup_open_to  # noqa: F401 (re-exported)
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
    # RC10 (2026-11-19): a code works on any storefront that takes one, and the account is made there. Checked here,
    # not left to callers, so the API and the web form refuse alike (security review 2026-11-19)
    if not takes_referral_codes(storefront):
        return None
    return user if under_caps(user) else None


def benefit_on(referrer: User, code: PromoCode, domain: DomainInfo):
    """What `referrer`'s code row gives someone joining `domain` (RC10, 2026-11-19): its own gift or discount on the
    referrer's home domain, where it was chosen from that domain's tiers; on any other, the site's default there,
    worked out without being kept, so the code's own choice survives a use elsewhere."""
    benefit = referral_benefit(code, domain) if domain.id == referrer.home_domain().id \
        else default_referral_benefit(domain)
    # A gift admits as a membership of `domain`: of a tier not sold there it gives nothing, and the signup is refused,
    # rather than an account made for free on a paid domain (security review 2026-11-20). Not stored: a tier missing
    # for a while (its provider down) must not replace the code's own choice.
    if benefit is not None and benefit.offer is not None and not tier_sold_on(benefit.offer, domain):
        return None
    return benefit


def record_referral(referrer: User, referred: User) -> Referral:
    """Add the who-referred-whom row for `referred`, made with `referrer`'s current code, and where `referred`'s domain
    charges, the gift that code gives (paid membership PM3, G2, G4; plan 2026-11-16 R1, R4). A code that gives a
    discount gave it at registration, held for the first payment. The caller commits."""
    row = Referral(referrer_id=referrer.id, referred=referred, code=referrer.referral_code, created_at=utcnow())
    db.session.add(row)
    code = referral_gift_code(referrer)
    if code is None or not paid_registration(referred.home_domain()):
        return row
    benefit = benefit_on(referrer, code, referred.home_domain())
    # Nothing to give (X2), or a gift or a discount it can no longer give, refuses the account rather than letting it
    # in for free, which complimentary membership would then make free for good (R5, X2; audit
    # 2026-11-16): the caller rolls back, as for a gift code typed at registration
    if benefit is None or not usable(code):
        raise GiftUnavailable()
    # A discount is held for a paying account's first payment (R4): an account made without going to payment (the API's
    # signup, which has no checkout) cannot take one, or it would be in for free (security review 2026-11-19)
    if benefit.discount is not None and referred.registration_pending_since is None:
        raise GiftUnavailable()
    db.session.flush()   # the gift's subscription, or the use, names the new account
    if benefit.offer is not None:
        if start_gift(referred, code, commit=False, offer=benefit.offer, periods=benefit.periods) is None:
            raise GiftUnavailable()
    else:   # a use of the referral code, as a gift is (R5)
        db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=referred.id, used_at=utcnow()))
    return row


def referral_discount(referrer: User, domain: DomainInfo) -> PromoCode | None:
    """The discount code `referrer`'s referral code gives, if it gives one (R1, R4). The discount's own limits are the
    caller's to ask, since one past them refuses the registration rather than admitting for free (R5). The referral
    code's own (its switch, last day and uses) are asked here: past them it gives no discount, as it would give no
    gift, and the caller refuses. Under referrer_for's lock on the referrer, so its uses can't be overtaken."""
    code = referral_gift_code(referrer)
    benefit = benefit_on(referrer, code, domain) if code is not None else None
    return benefit.discount if benefit is not None else None


def referral_discount_refused(referrer: User, domain: DomainInfo) -> bool:
    """Whether `referrer`'s referral code gives a discount on `domain` but is past its own limits (security review
    2026-11-16)."""
    code = referral_gift_code(referrer)
    return code is not None and referral_discount(referrer, domain) is not None and not usable(code)


def takes_referral_codes(storefront: DomainInfo) -> bool:
    """Whether a referral code can be used to join on `storefront`: one whose signup a code opens (RC11, RC11a). A
    domain where joining is paid for takes one too (owner 2026-11-20; was the default domain alone, G4): there it
    admits with what it gives on that domain (`benefit_on`), and one that gives nothing there admits nobody."""
    return signup_open_to(storefront, True)


def referral_domain(user: User) -> DomainInfo:
    """RC9 (2026-11-19): the storefront `user`'s link registers people on: the one this request came in on, which is
    where a code makes the account (RC10), when it takes codes; else the referrer's home storefront."""
    here = current_storefront()
    return here if takes_referral_codes(here) else user.home_domain()


def referral_link(user: User, domain: DomainInfo | None = None) -> str:
    """RC9: the registration URL on `domain` (by default `referral_domain(user)`)."""
    domain = domain or referral_domain(user)
    return domain_base_url(domain) + url_for('auth.register', ref=user.referral_code)


def referral_qr_svg(link: str) -> str:
    return segno.make(link, error='m').svg_inline(scale=5, title='QR code', border=2, dark='#000', light='#fff')


def referral_qr_png(link: str) -> bytes:
    out = io.BytesIO()
    segno.make(link, error='m').save(out, kind='png', scale=8, border=2)
    return out.getvalue()


def referrals_available(user) -> bool:
    """Whether `user` gets the referral-code settings page: referral mode is on and they may generate codes."""
    return getattr(user, 'is_authenticated', False) and referral_mode_on(g.site) and may_generate(user)


def assigned_codes(user) -> list[PromoCode]:
    """The gift and discount codes an admin made for `user` to hand out (owner 2026-11-16), oldest first. Not
    referrals: ordinary codes, listed on the referral page. Its callers are signed in."""
    return PromoCode.query.filter_by(assigned_to=user.id).order_by(PromoCode.created_at, PromoCode.id).all()


def referral_page_available(user) -> bool:
    """Whether `user` gets the referral settings page: for their own referral code, or for codes made for them."""
    return referrals_available(user) or (getattr(user, 'is_authenticated', False) and user.ap_id is None
                                         and not user.deleted and db.session.query(
                                             PromoCode.query.filter_by(assigned_to=user.id).exists()).scalar())
