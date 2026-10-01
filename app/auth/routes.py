from datetime import datetime

from flask import flash, g, make_response, redirect, request, url_for, current_app, session
from flask_babel import _
from flask_login import current_user, login_user, logout_user
from sqlalchemy import func

from app import db, limiter, oauth
from app.auth import bp
from app.auth.forms import LoginForm, RegisterByMastodonForm, RegistrationForm, ResetPasswordForm, \
    ResetPasswordRequestForm, ResendEmailForm
from app.auth.oauth_util import (
    handle_oauth_authorize,
    email_already_registered,
    finalize_user_login,
    initialize_new_user,
    refuse_banned_visitor,
)
from app.auth.util import (
    handle_abandoned_open_instance,
    notify_admins_of_registration,
    process_login,
    process_registration_form,
    redirect_next_page,
    render_login_form,
    render_registration_form,
    handle_banned_user,
    get_country,
    send_email_verification,
    random_token,
)
from app.email import send_password_reset_email, send_registration_approved_email
from app.models import User, UserRegistration
from app.shared.tasks import task_selector
from app.utils import (
    finalize_user_setup,
    render_template,
    login_required,
    ip_address,
    user_ip_banned,
    user_cookie_banned,
)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("300 per day;10 per 5 minutes", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect_next_page()

    form = LoginForm()
    if request.method == "POST" and form.validate_on_submit():
        return process_login(form)

    return render_login_form(form)


@bp.route('/logout')
def logout():
    logout_user()
    response = make_response(redirect(url_for('main.index')))
    response.set_cookie('low_bandwidth', '0', expires=datetime(year=2099, month=12, day=30))
    return response


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("300 per day;10 per 5 minutes", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    handle_abandoned_open_instance()

    form = RegistrationForm()
    if g.site.tos_url is None or not g.site.tos_url.strip():
        del form.terms
    if g.site.registration_mode == 'Open':
        del form.question
    if request.method == "POST" and form.validate_on_submit():
        return process_registration_form(form)

    return render_registration_form(form)


@bp.route('/please_wait', methods=['GET'])
def please_wait():
    return render_template('auth/please_wait.html', title=_('Account under review'))


@bp.route('/not_trustworthy', methods=['GET'])
def not_trustworthy():
    return render_template('generic_message.html', title=_('Sorry'), message=_('You are not able to do this action due to being a very new account or because of a low reputation.'))


@bp.route('/check_email', methods=['GET'])
def check_email():
    return render_template('auth/check_email.html', title=_('Check your email'))


@bp.route('/resend_email', methods=['GET', 'POST'])
@limiter.limit("300 per day;10 per 5 minutes", methods=['POST'])
def resend_email():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = ResendEmailForm()
    if form.validate_on_submit():
        form.email.data = form.email.data.strip()
        user = User.query.filter(func.lower(User.email) == func.lower(form.email.data)).filter_by(ap_id=None, deleted=False).first()

        if user is None:
            # D1129. This said "No user found with that email address." while
            # the success path says "If an account exists, a link has been
            # sent" -- so the pair of answers told a caller which addresses
            # are registered here, one guess at a time. `reset_password_request`
            # next door already answers the same way either way; this end did
            # not (fact 478). Measured: `PROBE ar2 unknown address says: True`.
            flash(_("If an account exists, a link has been sent"))
            return redirect(url_for('auth.check_email'))

        # Create verification token if it doesn't exist already or else verification is impossible
        if not user.verification_token:
            user.verification_token = random_token(16)
            db.session.commit()

        # D1164. An `if user:` stood here, below the `if user is None: return`
        # eight lines above it: an arm that could not be false.
        try:
            send_email_verification(user)
            flash(_("If an account exists, a link has been sent"))
            return redirect(url_for('auth.check_email'))
        except Exception:
            flash(_("Problem sending email, please contact the administrator for support"), 'warning')
            return redirect(url_for('auth.resend_email'))
    
    return render_template('auth/resend_email_request.html', title=_('Resend verification email'), form=form)


@bp.route('/reset_password_request', methods=['GET', 'POST'])
@limiter.limit("300 per day;10 per 5 minutes", methods=['POST'])
def reset_password_request():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        form.email.data = form.email.data.strip()
        if form.email.data.lower().startswith('postmaster@') or form.email.data.lower().startswith('abuse@') \
                or form.email.data.lower().startswith('noc@'):
            flash(_('Sorry, you cannot use that email address.'), 'error')
        else:
            user = User.query.filter(func.lower(User.email) == func.lower(form.email.data)).filter_by(ap_id=None, deleted=False).first()
            if user:
                send_password_reset_email(user)
                flash(_('If an account exists, a link has been sent.'))
                return redirect(url_for('auth.login'))
            else:
                flash(_('If an account exists, a link has been sent'), 'warning')
    return render_template('auth/reset_password_request.html', title=_('Reset Password'), form=form)


@bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    user = User.verify_reset_password_token(token)
    if not user:
        return redirect(url_for('main.index'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash(_('Your password has been reset. Please use it to log in with user name of %(name)s.', name=user.user_name))
        return redirect(url_for('auth.login'))
    return render_template('auth/reset_password.html', form=form, domain=current_app.config['SERVER_NAME'])


@bp.route('/verify_email/<token>')
def verify_email(token):
    # D1165. `if token != '':` wrapped this whole body, and nothing else --
    # no else arm, so had it ever been false the view would have returned
    # None: `TypeError: The view function ... did not return a valid
    # response`. It never was: Flask's default converter does not match an
    # empty path segment, so `/verify_email/` is a 404 and `token` is never
    # the empty string here.
    user = User.query.filter_by(verification_token=token).first()
    if user is not None:
        if user.banned:
            flash(_('You have been banned.'), 'error')
            return redirect(url_for('main.index'))
        if user.verified:  # guard against users double-clicking the link in the email
            flash(_('Thank you for verifying your email address.'))
            return redirect(url_for('auth.login'))
        user.verified = True
        user.verification_token = random_token(16)

        # Update any pending application status from -1 to 0 when email is verified
        application = UserRegistration.query.filter_by(user_id=user.id, status=-1).first()
        if application:
            application.status = 0

            # Now notify admins since application is ready for review
            notify_admins_of_registration(application)

        db.session.commit()
        if not user.waiting_for_approval() and user.private_key is None:  # only finalize user set up if this is a brand new user. People can also end up doing this process when they change their email address in which case we DO NOT want to reset their keys, etc!
            finalize_user_setup(user)
        flash(_('Thank you for verifying your email address.'))
    else:
        flash(_('Email address validation failed.'), 'error')
        return redirect(url_for('main.index'))

    if user.waiting_for_approval():
        return redirect(url_for('auth.please_wait'))
    else:
        # Two things need to happen - email verification and (usually) admin approval.
        if g.site.registration_mode == 'RequireApplication':
            send_registration_approved_email(user)
        else:
            ...
            # send_welcome_email(user) #not written yet

        login_user(user, remember=True)
        if user.communities():
            return redirect(url_for('main.index'))
        else:
            return redirect(url_for('auth.filter_selection'))


@bp.route('/validation_required')
def validation_required():
    return render_template('auth/validation_required.html')


@bp.route('/permission_denied')
def permission_denied():
    return render_template('auth/permission_denied.html')


@bp.route("/google_login")
def google_login():
    # If user is already logged in, redirect to connect route
    if current_user.is_authenticated:
        return redirect(url_for('auth.google_connect'))
    return oauth.google.authorize_redirect(redirect_uri=url_for('auth.google_authorize', _external=True))


@bp.route("/google_connect")
@login_required
def google_connect():
    return oauth.google.authorize_redirect(redirect_uri=url_for('auth.google_connect_callback', _external=True))


@bp.route("/google_authorize")
def google_authorize():
    return handle_oauth_authorize(
        provider='google',
        user_info_endpoint='oauth2/v2/userinfo',
        oauth_id_key='google_oauth_id'
    )


@bp.route("/google_connect_callback")
@login_required
def google_connect_callback():
    try:
        token = oauth.google.authorize_access_token()
        resp = oauth.google.get('oauth2/v2/userinfo', token=token)
        user_info = resp.json()

        # Check if this OAuth ID is already connected to another account
        existing_user = User.query.filter_by(google_oauth_id=user_info['id']).first()
        if existing_user and existing_user.id != current_user.id:
            flash(_('This Google account is already connected to another user.'), 'error')
            return redirect(url_for('user.connect_oauth'))

        # Connect the OAuth ID to the current user
        current_user.google_oauth_id = user_info['id']
        db.session.commit()

        flash(_('Your Google account has been connected successfully.'), 'success')
        return redirect(url_for('user.connect_oauth'))
    except Exception as e:
        # D1136. This used to put `str(e)` in front of the person. The
        # exception comes from the OAuth client, and its text carries
        # whatever the library chose to put there -- a token, a URL with a
        # code in it, an internal address. Measured: a flash reading
        # `Failed to connect Google account: token secret=abc123 leaked`.
        # The detail belongs in the log, where the operator can see it and
        # the visitor cannot.
        current_app.logger.warning('Google OAuth connect failed: %s', e)
        flash(_('Failed to connect Google account. Please try again.'), 'error')
        return redirect(url_for('user.connect_oauth'))


@bp.route("/mastodon_login")
def mastodon_login():
    # If user is already logged in, redirect to the connect route
    if current_user.is_authenticated:
        return redirect(url_for('auth.mastodon_connect'))
    return oauth.mastodon.authorize_redirect(redirect_uri=url_for('auth.mastodon_authorize', _external=True))


@bp.route("/mastodon_authorize", methods=["GET", "POST"])
def mastodon_authorize():
    oauth_id_key = 'mastodon_oauth_id'
    form = RegisterByMastodonForm()
    if session.get("user_info") is not None:
        if request.method == "POST" and form.validate_on_submit():
            # User is submitting the registration form after OAuth authorization
            user_info = session["user_info"]
            session.pop("user_info", None)
            user = User.query.filter_by(**{oauth_id_key: user_info['id']}).first()
            ip = ip_address()
            country = get_country(ip)
            if user:
                if not user.is_ban_exempt() and (user.banned or user_ip_banned() or user_cookie_banned()):
                    return handle_banned_user(user, ip)
                elif user.deleted:
                    flash(_('This account has been deleted.'), 'error')
                    return redirect(url_for('auth.login'))
                else:
                    # User already exists, finalize login
                    return finalize_user_login(user, None, ip, country)
            else:
                refusal = refuse_banned_visitor()  # D1142
                if refusal is not None:
                    return refusal
                email = form.email.data.strip()
                if email_already_registered(email):  # D1141
                    session["user_info"] = user_info  # they get another try
                    form.email.errors.append(
                        _('An account with this email address already exists.'))
                    return render_template('auth/mastodon_authorize.html',
                                           form=form, user_info=user_info)
                username = user_info['username']
                # New user registration
                user = initialize_new_user(
                    email, username, oauth_id_key, user_info, ip, country
                )
                if (
                    g.site.registration_mode == "RequireApplication"
                    and g.site.application_question
                ):
                    task_selector(
                        "check_application",
                        application_id=user.registration_application.id,
                    )
                    return redirect(url_for("auth.please_wait"))
                return redirect_next_page() if len(user.communities()) >= 0 else redirect(url_for('auth.filter_selection'))
        elif request.method == "POST":
            # D1141's other half. A POST that does not validate used to fall
            # through to `handle_oauth_authorize`, which asks the provider for
            # a token it has already spent -- so a visitor whose email was
            # refused was told "Login failed due to a problem with the OAuth
            # server" instead of being shown what was wrong with the form.
            return render_template('auth/mastodon_authorize.html', form=form,
                                   user_info=session["user_info"])

    return handle_oauth_authorize(
        provider='mastodon',
        user_info_endpoint='accounts/verify_credentials',
        oauth_id_key=oauth_id_key,
        form_class=RegisterByMastodonForm
    )


@bp.route("/mastodon_connect")
@login_required
def mastodon_connect():
    # D1135. `google_connect`, `discord_connect` and both of their callbacks
    # carry `@login_required`; these two did not. Connecting an account to
    # `current_user` is meaningless without one, and the anonymous caller got
    # as far as being redirected to the remote instance -- a real
    # authorization burned on a flow that can only fail. Measured:
    # `PROBE au1 anonymous /auth/mastodon_connect: 200`, against 302 to the
    # login page for all four siblings. Fact 478 again.
    return oauth.mastodon.authorize_redirect(redirect_uri=url_for('auth.mastodon_connect_callback', _external=True))


@bp.route("/mastodon_connect_callback")
@login_required
def mastodon_connect_callback():
    """
    Callback route for Mastodon OAuth connection.
    This is used when a user is already logged in and wants to connect their Mastodon account.
    """
    try:
        token = oauth.mastodon.authorize_access_token()
        resp = oauth.mastodon.get('accounts/verify_credentials', token=token)
        user_info = resp.json()

        # Check if this OAuth ID is already connected to another account
        existing_user = User.query.filter_by(mastodon_oauth_id=user_info['id']).first()
        if existing_user and existing_user.id != current_user.id:
            flash(_('This Mastodon account is already connected to another user.'), 'error')
            return redirect(url_for('user.connect_oauth'))

        # Connect the OAuth ID to the current user
        current_user.mastodon_oauth_id = user_info['id']
        db.session.commit()

        flash(_('Your Mastodon account has been connected successfully.'), 'success')
        return redirect(url_for('user.connect_oauth'))
    except Exception as e:
        # D1136. This used to put `str(e)` in front of the person. The
        # exception comes from the OAuth client, and its text carries
        # whatever the library chose to put there -- a token, a URL with a
        # code in it, an internal address. Measured: a flash reading
        # `Failed to connect Mastodon account: token secret=abc123 leaked`.
        # The detail belongs in the log, where the operator can see it and
        # the visitor cannot.
        current_app.logger.warning('Mastodon OAuth connect failed: %s', e)
        flash(_('Failed to connect Mastodon account. Please try again.'), 'error')
        return redirect(url_for('user.connect_oauth'))


@bp.route("/discord_login")
def discord_login():
    # If user is already logged in, redirect to connect route
    if current_user.is_authenticated:
        return redirect(url_for('auth.discord_connect'))
    return oauth.discord.authorize_redirect(redirect_uri=url_for('auth.discord_authorize', _external=True))


@bp.route("/discord_connect")
@login_required
def discord_connect():
    return oauth.discord.authorize_redirect(redirect_uri=url_for('auth.discord_connect_callback', _external=True))


@bp.route("/discord_authorize")
def discord_authorize():
    return handle_oauth_authorize(
        provider="discord",
        user_info_endpoint="users/@me",
        oauth_id_key="discord_oauth_id",
    )


@bp.route("/discord_connect_callback")
@login_required
def discord_connect_callback():
    try:
        token = oauth.discord.authorize_access_token()
        resp = oauth.discord.get("users/@me", token=token)
        user_info = resp.json()

        # Check if this OAuth ID is already connected to another account
        existing_user = User.query.filter_by(discord_oauth_id=user_info['id']).first()
        if existing_user and existing_user.id != current_user.id:
            flash(_('This Discord account is already connected to another user.'), 'error')
            return redirect(url_for('user.connect_oauth'))

        # Connect the OAuth ID to the current user
        current_user.discord_oauth_id = user_info['id']
        db.session.commit()

        flash(_('Your Discord account has been connected successfully.'), 'success')
        return redirect(url_for('user.connect_oauth'))
    except Exception as e:
        # D1136. This used to put `str(e)` in front of the person. The
        # exception comes from the OAuth client, and its text carries
        # whatever the library chose to put there -- a token, a URL with a
        # code in it, an internal address. Measured: a flash reading
        # `Failed to connect Discord account: token secret=abc123 leaked`.
        # The detail belongs in the log, where the operator can see it and
        # the visitor cannot.
        current_app.logger.warning('Discord OAuth connect failed: %s', e)
        flash(_('Failed to connect Discord account. Please try again.'), 'error')
        return redirect(url_for('user.connect_oauth'))
