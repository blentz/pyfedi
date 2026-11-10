from flask_smorest import abort

from app import db, cache
from app.auth.util import find_user, get_country
from app.ldap_utils import sync_user_to_ldap
from app.storefront import paid_registration
from app.models import IpBan, utcnow, dummy_password_hash, verify_password_hash
from app.onion import apply_audit, is_onion_request, stamp_activity
from app.onion_account import audit_country, audit_ip, is_onion_account
from app.onion_guard import onion_login_gate, onion_login_succeeded, onion_wait_message
from app.utils import ip_address, user_ip_banned, user_cookie_banned, banned_ip_addresses, log_login_failure


# D582: the API's login. The web logs in through app.auth.util.log_user_in.
def api_log_user_in(input):
    user = log_in_with_password(input['username'], input['password'])
    login_json = {'jwt': user.encode_jwt_token()}
    return login_json


def log_in_with_password(username: str, password: str):
    """The account a username (or email, or profile URL) and password log in to, with every check and record of the
    API's login: raises Exception('incorrect_login') for any refusal, and aborts 429 while the onion gate waits. Also
    PeerTube's password grant (spec 2026-11-09-peertube-login-design §3, ruling P1)."""
    ip = ip_address()
    username = username.lower().strip()
    password = password.strip()

    # D584: the web login's finder, so both accept the same forms
    user = find_user(username)
    # Onion only (spec 3.7): failed logins slow down what was typed and the account it names (OnionLoginGate)
    gate = onion_login_gate(username, user)
    if gate.wait:
        abort(429, message=str(onion_wait_message(gate.wait)))

    if not user:
        # No match for username, email or profile url was found. A wrong password's hash work, or the time taken
        # tells which names have accounts.
        verify_password_hash(dummy_password_hash(), password)
        log_login_failure()
        raise Exception('incorrect_login')

    # (onion) an account in hidden backoff is verified all the same and refused: a wrong password's answer and cost
    if not user.check_password(password, refuse=gate.hidden):
        log_login_failure()
        raise Exception('incorrect_login')

    if user.registration_pending_since is not None:   # an account that hasn't paid its first fee can't log in (M10)
        raise Exception('incorrect_login')

    if not user.is_ban_exempt() and (user.banned or user_ip_banned() or user_cookie_banned()):
        # Detect if a banned user tried to log in from a new IP address
        if user.banned and not user_ip_banned() and not is_onion_request() and not is_onion_account(user):
            # If so, ban their new IP address as well
            new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
            db.session.add(new_ip_ban)
            db.session.commit()
            cache.delete_memoized(banned_ip_addresses)

        raise Exception('incorrect_login')

    onion_login_succeeded(username, user)
    stamp_activity(user)
    apply_audit(user, 'ip_address', audit_ip(user, ip))
    apply_audit(user, 'ip_address_country', audit_country(user, get_country(ip, user=user)))
    db.session.commit()

    try:
        if not paid_registration(user.home_domain()):   # the directory is no way round a domain's fee (plan S11)
            sync_user_to_ldap(user.user_name, user.email, password.strip())
    except Exception:
        ...

    return user
