import re
from random import randint

from flask import flash, g, redirect, render_template, request, url_for, session, current_app
from flask_babel import _
from flask_login import login_user, current_user
from sqlalchemy import func

from app import db, oauth
from app.auth.util import create_registration_application, get_country, handle_banned_user
from app.models import User, utcnow
from app.shared.tasks import task_selector
from app.utils import RESERVED_USER_NAMES, actor_contains_blocked_words, finalize_user_setup, get_setting, gibberish, \
    ip_address, user_cookie_banned, user_ip_banned, user_name_is_taken


def is_country_blocked(country: str) -> bool:
    """
    Checks if the user's country is blocked based on IP or settings.
    """
    if country:
        for country_code in get_setting('auto_decline_countries', '').split('\n'):
            if country_code.strip().upper() == country.upper():
                return True
    return False


def handle_user_verification(user, oauth_id_key, token, ip, country, user_info):
    """
    Handles user verification and registration logic.
    """
    if not user:
        email = user_info.get('email')
        username = user_info.get('username', '')

        # D1143. Not every provider hands back an email -- Mastodon's
        # `accounts/verify_credentials` does not -- and `email.lower()` below
        # answered `AttributeError: 'NoneType' object has no attribute
        # 'lower'` when one did not. Measured: `PROBE av7 outcome:
        # AttributeError: 'NoneType' object has no attribute 'lower'`.
        if not email:
            flash(_('That account did not give us an email address, so it '
                    'cannot be used to sign up here.'), 'error')
            return redirect(url_for('auth.login'))

        refusal = refuse_banned_visitor()  # D1142
        if refusal is not None:
            return refusal

        # Check if an account with this email already exists
        # Otherwise
        if email_already_registered(email):
            flash(_('An account with this email already exists, please login and connect this account over "Connect OAuth" setting.'), 'error')
            return redirect(url_for('auth.login'))

        # Register a new user
        user = initialize_new_user(email, username, oauth_id_key, user_info, ip, country)
        if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
            task_selector('check_application', application_id=user.registration_application.id)
            return redirect(url_for('auth.please_wait'))
        # D1138. This used to `return None`, and a Flask view that returns
        # None is `TypeError: The view function ... did not return a valid
        # response`. So EVERY first-time Google or Discord sign-up ended on an
        # error page -- after the account had been created and logged in.
        # Measured: `PROBE av4 outcome: TypeError: ... | user created: True`.
        # The local registration path sends a new account to the same place.
        return redirect(url_for('auth.filter_selection'))
    else:
        # Handle existing user
        return finalize_user_login(user, token, ip, country)


def email_already_registered(email):
    """D1141. One address, one account.

    The providers that supply an email have always been asked this question.
    Mastodon supplies none, so its arm asks the visitor to type one -- and
    nothing checked it, so two accounts ended up holding one address.
    Measured:

        PROBE av6 status: 302 | accounts holding that email: 2 |
        names: ['Person', 'person']

    Login-by-email, the reset-password request and the resend-verification
    form all look an account up by address and take `.first()`, so a
    duplicate decides which of the two those answer for.
    """
    return User.query.filter(func.lower(User.email) == func.lower(email.strip())).first() is not None


def refuse_banned_visitor():
    """D1142. A response when this visitor may not register, otherwise None.

    `handle_oauth_authorize` sends an EXISTING banned account to
    `handle_banned_user`, and the Mastodon form arm does the same. A visitor
    who has no account yet was never asked: `initialize_new_user` wrote
    `banned=user_ip_banned() or user_cookie_banned()` into the row and then
    called `finalize_user_setup` and `login_user` on it regardless, so a
    banned IP got a working session on a fresh account. Measured:

        PROBE av5 outcome: ... | created: True | banned: True

    Both new-account sites call this, rather than each carrying its own copy.
    """
    if user_ip_banned() or user_cookie_banned():
        flash(_('You have been banned.'), 'error')
        return redirect(url_for('auth.login'))
    return None


def initialize_new_user(email, username, oauth_id_key, user_info, ip, country):
    """
    Creates and registers a new user.
    """
    user = User(
        user_name=find_new_username(email),
        email=email,
        title=username,
        verified=True,
        verification_token='',
        instance_id=1,
        ip_address=ip,
        ip_address_country=country,
        banned=user_ip_banned() or user_cookie_banned(),
        alt_user_name=gibberish(randint(8, 20)),
    )
    if user.title is None or user.title.strip() == '':  # ensure user has a display name. Sometimes OAuth doesn't give us one
        user.title = user.user_name
    if current_app.config['CONTENT_WARNING']:
        user.hide_nsfw = 0
    setattr(user, oauth_id_key, user_info['id'])  # Assign OAuth provider ID
    db.session.add(user)
    db.session.commit()

    # Handle registration mode requiring applications
    if g.site.registration_mode == 'RequireApplication' and g.site.application_question:
        user.registration_application = create_registration_application(user, f"Signed in with {oauth_id_key.title()}")
        db.session.commit()
        return user
    else:
        # Finalize registration and log user in
        finalize_user_setup(user)
        login_user(user, remember=True)
        return user


def get_token_and_user_info(provider, user_info_endpoint):
    """
    Retrieve OAuth token and user information for the provider.
    """
    try:
        oauth_provider = getattr(oauth, provider, None)
        if not oauth_provider:
            raise ValueError(f"OAuth provider '{provider}' is not configured.")

        token = oauth_provider.authorize_access_token()

        resp = oauth_provider.get(user_info_endpoint, token=token)
        return token, resp.json()
    except Exception:
        return None, None


def current_user_is_banned():
    """
    Check if the current user is banned.
    """
    if not current_user.is_authenticated:
        return False
    return current_user.banned or user_ip_banned() or user_cookie_banned()


def current_user_is_deleted():
    """
    Check if the current user account is deleted.
    """
    if not current_user.is_authenticated:
        return False
    return current_user.deleted


def can_user_register():
    """
    Check if the user can register or login based on the site's registration mode.
    """
    if g.site.registration_mode == 'Closed':
        flash(_('Account registrations are currently closed.'), 'error')
        return redirect(url_for('auth.login'))
    if g.site.registration_mode == 'RequireApplication' and not g.site.application_question:
        flash(_('Account registrations are currently closed.'), 'error')
        return redirect(url_for('auth.login'))
    if is_country_blocked(get_country(ip_address())):
        flash(_('Application declined'), 'error')
        return render_template('generic_message.html', title=_('Application declined'),
                               message=_('Sorry, we are not accepting registrations from your country.'))
    return True


def handle_oauth_authorize(provider, user_info_endpoint, oauth_id_key, form_class=None):
    """
    Generalized handler for OAuth authorize routes.
    """
    token, user_info = get_token_and_user_info(provider, user_info_endpoint)
    if not token or not user_info:
        flash(_('Login failed due to a problem with the OAuth server.'), 'error')
        return redirect(url_for('auth.login'))

    # D1144. `can_user_register` answers True, a redirect or a rendered page --
    # never False -- so the `is False` arm this replaces could not be taken,
    # and it existed to turn a False into the redirect that the function
    # already returns for itself.
    can_user_authenticate = can_user_register()
    if can_user_authenticate is not True:
        return can_user_authenticate

    ip = ip_address()
    country = get_country(ip)
    user = User.query.filter(getattr(User, oauth_id_key) == user_info['id']).first()
    if user:
        if not user.is_ban_exempt() and (user.banned or user_ip_banned() or user_cookie_banned()):
            return handle_banned_user(user, ip)
        elif user.deleted:
            flash(_('This account has been deleted.'), 'error')
            return redirect(url_for('auth.login'))

    if not user:
        form = form_class() if form_class else None
        # For providers requiring a registration form
        if form_class and (request.method == "GET" or not form.validate_on_submit()):
            # session['code'] = request.args.get('code')
            session["user_info"] = user_info
            return render_template(f'auth/{provider}_authorize.html', form=form, user_info=user_info)

    return handle_user_verification(user, oauth_id_key, token, ip, country, user_info)


def finalize_user_login(user, token, ip, country):
    """
    Performs final steps of logging in a user once verified.
    """
    user.last_seen = utcnow()
    user.ip_address = ip
    user.ip_address_country = country
    db.session.commit()

    login_user(user, remember=True)
    return redirect(url_for('main.index'))


def find_new_username(email: str) -> str:
    """The user name an OAuth signup gets, from the email's local part.

    D1139/D1140. This used to answer that local part VERBATIM, checking only
    that no user held the same string with the same capitalisation. Every
    other rule `RegistrationForm.validate_user_name` enforces was missing, so
    an OAuth sign-in minted names the registration form refuses outright.
    Measured:

        PROBE av1 admin exists: True
        PROBE av2 names: ['founder', 'Person', 'we.ird+chars!']
        PROBE av3 person-ish names: ['Person', 'person']

    `admin` is the name the registration path reserves; `we.ird+chars!` is
    outside USER_NAME_CHARSET_RE, which exists because a local user name is
    interpolated into an actor URL, a webfinger response and a feed regex;
    and `person` beside `Person` is a second account answering to one name,
    which `find_user` resolves with `.first()`.

    Fact 478 once more -- registration has two ends, and only one of them
    asked any of these questions.
    """
    base = re.sub(r'[^a-z0-9_]', '', email.lower().split('@')[0])
    # Length(min=3) is the form's rule. A base that cannot satisfy the
    # charset, the reserved list or the blocked words is replaced outright
    # rather than decorated, so the loop below only ever answers one question.
    if len(base) < 3 or base in RESERVED_USER_NAMES or \
            actor_contains_blocked_words(base):
        base = gibberish(10)

    candidate = base
    attempts = 0
    while attempts < 1000:
        if not user_name_is_taken(candidate):
            return candidate
        candidate = base + str(randint(1, 1000))
        attempts += 1

    return gibberish(10)
