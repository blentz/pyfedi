from flask_smorest import abort

from app import db, cache
from app.auth.util import find_user, get_country
from app.ldap_utils import sync_user_to_ldap
from app.models import IpBan, utcnow
from app.onion import audit_field, is_onion_request
from app.onion_guard import login_counters, onion_login_succeeded, onion_login_wait
from app.utils import ip_address, user_ip_banned, user_cookie_banned, banned_ip_addresses


# D582: the API's login. The web logs in through app.auth.util.log_user_in.
def api_log_user_in(input):
    ip = ip_address()
    country = get_country(ip)
    username = input['username'].lower().strip()
    password = input['password'].strip()

    # D584: the web login's finder, so both accept the same forms
    user = find_user(username)
    # Onion only (spec 3.7): failed logins slow down what was typed and the account it names (login_counters)
    counters = login_counters(username, user)
    wait = onion_login_wait(*counters)
    if wait:
        abort(429, message=f'Too many failed logins for this account. Try again in {wait} seconds.')

    if not user:
        # No match for username, email or profile url was found
        raise Exception('incorrect_login')

    if not user.check_password(password):
        raise Exception('incorrect_login')

    if not user.is_ban_exempt() and (user.banned or user_ip_banned() or user_cookie_banned()):
        # Detect if a banned user tried to log in from a new IP address
        if user.banned and not user_ip_banned() and not is_onion_request():
            # If so, ban their new IP address as well
            new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
            db.session.add(new_ip_ban)
            db.session.commit()
            cache.delete_memoized(banned_ip_addresses)

        raise Exception('incorrect_login')

    onion_login_succeeded(*counters)
    user.last_seen = utcnow()
    user.ip_address = audit_field(ip)
    user.ip_address_country = audit_field(country)
    db.session.commit()

    try:
        sync_user_to_ldap(user.user_name, user.email, password.strip())
    except Exception:
        ...

    login_json = {'jwt': user.encode_jwt_token()}
    return login_json
