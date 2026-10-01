from sqlalchemy import func

from app import db, cache
from app.auth.util import get_country
from app.ldap_utils import sync_user_to_ldap
from app.models import IpBan, User, utcnow
from app.utils import ip_address, user_ip_banned, user_cookie_banned, banned_ip_addresses


# D582: the API's login. The web logs in through app.auth.util.log_user_in.
def api_log_user_in(input):
    ip = ip_address()
    country = get_country(ip)
    username = input['username'].lower().strip()
    password = input['password'].strip()

    user = db.session.query(User).filter(func.lower(User.user_name) == func.lower(username)).filter_by(ap_id=None, deleted=False).first()

    if not user:
        # user is None if no match was found
        user = db.session.query(User).filter(func.lower(User.email) == func.lower(username)).filter_by(ap_id=None, deleted=False).first()

    if not user:
        # No match for username or email was found
        raise Exception('incorrect_login')

    if not user.check_password(password):
        raise Exception('incorrect_login')

    if not user.is_ban_exempt() and (user.banned or user_ip_banned() or user_cookie_banned()):
        # Detect if a banned user tried to log in from a new IP address
        if user.banned and not user_ip_banned():
            # If so, ban their new IP address as well
            new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
            db.session.add(new_ip_ban)
            db.session.commit()
            cache.delete_memoized(banned_ip_addresses)

        raise Exception('incorrect_login')

    user.last_seen = utcnow()
    user.ip_address = ip
    user.ip_address_country = country
    db.session.commit()

    try:
        sync_user_to_ldap(user.user_name, user.email, password.strip())
    except Exception:
        ...

    login_json = {'jwt': user.encode_jwt_token()}
    return login_json
