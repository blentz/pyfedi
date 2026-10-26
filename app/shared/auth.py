from flask_smorest import abort

from app import db, cache
from app.auth.util import find_user, get_country
from app.ldap_utils import sync_user_to_ldap
from app.models import IpBan, utcnow, dummy_password_hash, verify_password_hash
from app.onion import apply_audit, is_onion_request
from app.onion_account import audit_country, audit_ip, is_onion_account
from app.onion_guard import onion_login_gate, onion_login_succeeded, onion_wait_message
from app.utils import ip_address, user_ip_banned, user_cookie_banned, banned_ip_addresses, log_login_failure


# D582: the API's login. The web logs in through app.auth.util.log_user_in.
def api_log_user_in(input):
    ip = ip_address()
    username = input['username'].lower().strip()
    password = input['password'].strip()

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
    user.last_seen = utcnow()
    apply_audit(user, 'ip_address', audit_ip(user, ip))
    apply_audit(user, 'ip_address_country', audit_country(user, get_country(ip, user=user)))
    db.session.commit()

    try:
        sync_user_to_ldap(user.user_name, user.email, password.strip())
    except Exception:
        ...

    login_json = {'jwt': user.encode_jwt_token()}
    return login_json
