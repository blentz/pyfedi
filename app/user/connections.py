"""A member's connected accounts (spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md §3).

Connecting runs on the member's home domain, under that domain's app or the instance's (spec 18, C9, C10): each
service knows the redirect addresses registered with it. What the sign-in hands back before the member has chosen (a code verifier, several Pages' tokens) travels in the session sealed under
CONNECTIONS_SECRETS_KEY, since Flask's session cookie is signed but readable.
"""
import secrets

from flask import abort, flash, jsonify, make_response, redirect, request, session, url_for
from flask_babel import _
from flask_login import current_user

from app import db, storefront
from app.connections import KEY, SERVICES, Account, ServiceError, sealing_available
from app.connections.apps import current, enabled, enabled_services, for_domain
from app.connections import bluesky, facebook, instagram, youtube  # noqa: F401 (registers the services)
from app.connections import bluesky_hosted as hosted
from app.atproto import identity as atproto_identity
from app.connections.dispatch import disconnect, retry
from app.connections.tokens import seal_tokens
from app.connections.content import image_bytes
from app.models import File, SocialConnection, SocialCrosspost
from app.sealing import SealUnavailable, seal_with, unseal_with
from app.storefront import current_storefront
from app.user import bp
from app.utils import login_required, render_template, safe_redirect_target

FLOW = 'connections_flow'


def _home():
    """The member's home domain: its app is theirs, and their sign-ins come back there (C10)."""
    return storefront.domain_by_id(current_user.site_domain_id)


def _at_home() -> bool:
    return current_storefront().domain == _home().domain


def _service_or_404(service_name: str):
    service = SERVICES.get(service_name)
    if not enabled(service, current_user.site_domain_id) or not _at_home():
        abort(404)
    return service


def _own_connection_or_404(connection_id: int) -> SocialConnection:
    connection = db.session.get(SocialConnection, connection_id)
    if connection is None or connection.user_id != current_user.id:
        abort(404)
    return connection


def _redirect_uri(service_name: str) -> str:
    """On the member's home domain: the address registered with its app, or with the instance's (C9)."""
    return f'{storefront.domain_base_url(_home())}/user/settings/connections/{service_name}/callback'


def _bluesky_domain_or_404() -> int | None:
    """The domain asked: each domain is its own Bluesky client, with its own key or the instance's (C9)."""
    here = current_storefront().id
    if not enabled(bluesky.SERVICE, here):
        abort(404)
    return here


@bp.route('/connections/bluesky/client-metadata.json', methods=['GET'])
def bluesky_client_metadata():
    """PieFed's atproto OAuth client id is this document's address (spec §3): a PDS fetches it at each sign-in."""
    with for_domain(_bluesky_domain_or_404()):
        response = jsonify(bluesky.client_metadata())
    response.headers['Cache-Control'] = 'public, max-age=600'
    return response


@bp.route('/connections/media/<token>.jpg', methods=['GET'])
def instagram_media(token):
    """A picture of a post, as Instagram fetches it while publishing (spec section 6): at a signed address that lasts an
    hour, fitted into the aspect ratios Instagram takes. Anything else is a 404."""
    file_id = instagram.media_file_id(token)
    file = db.session.get(File, file_id) if file_id is not None else None
    data = image_bytes(file) if file is not None else None
    jpeg = instagram.fitted(data) if data else None
    if jpeg is None:
        abort(404)
    response = make_response(jpeg)
    response.headers['Content-Type'] = 'image/jpeg'
    response.headers['Cache-Control'] = 'private, max-age=3600'
    return response


@bp.route('/connections/bluesky/jwks.json', methods=['GET'])
def bluesky_jwks():
    """The public half of BLUESKY_OAUTH_KEY, which signs PieFed's client assertions."""
    with for_domain(_bluesky_domain_or_404()):
        response = jsonify(bluesky.jwks())
    response.headers['Cache-Control'] = 'public, max-age=600'
    return response


@bp.route('/user/settings/connections', methods=['GET'])
@login_required
def user_connections():
    rows = SocialConnection.query.filter_by(user_id=current_user.id).order_by(SocialConnection.id).all()
    home = current_user.site_domain_id
    connections = [(row, SERVICES.get(row.service), enabled(SERVICES.get(row.service), home)) for row in rows]
    with for_domain(home):   # Instagram's ways in follow the apps the domain has
        offers = [(service, variant) for service in enabled_services(home)
                  for variant in (service.variants or (None,))
                  if service.name != 'bluesky' or bluesky.client_key() is not None]   # an own account needs the key
    mine = hosted.hosted_of(current_user)
    create = None
    if mine is None and hosted.hosting_on(home):   # A9: the handle is derived, or the alternative offered
        try:
            label = hosted.own_label(current_user, request.args.get('label'))
        except hosted.HandleRefused as refused:   # only the member's own name, or one of its alternatives
            label = refused.suggestion
        create = {'label': label, 'handle': hosted.handle_of(current_user, label)}
    place = hosted.place_of(mine) if mine is not None and mine.slot_state == hosted.WAITING else None
    return render_template('user/connections.html', title=_('Connected accounts'), user=current_user,
                           connections=connections, offers=offers, main_domain=_home().domain,
                           on_main_domain=_at_home(), create=create, place=place, hosted=hosted,
                           identity=identity_of_member())


@bp.route('/user/settings/connections/<service_name>/connect', methods=['POST'])
@login_required
def user_connection_connect(service_name):
    service = _service_or_404(service_name)
    variant = request.form.get('variant') or None
    state = secrets.token_urlsafe(24)
    # a Bluesky account asked to be the member's identity: its sign-in asks for the wider scope (ATProto spec §5)
    pending = {'identity': True} if service.name == 'bluesky' and request.form.get('identity') == '1' else {}
    try:
        with for_domain(current_user.site_domain_id):
            if variant not in (service.variants or (None,)):
                abort(400)
            url = service.authorize_url(variant, state, _redirect_uri(service.name), pending,
                                        hint=request.form.get('hint', '')[:300])
    except ServiceError as failure:
        flash(_('%(service)s could not be reached: %(error)s', service=service.label, error=failure.message), 'error')
        return redirect(url_for('user.user_connections'))
    session[FLOW] = seal_with(KEY, {'state': state, 'service': service.name, 'variant': variant, 'pending': pending,
                                    'user_id': current_user.id,   # where a sign-in started elsewhere returns (§8)
                                    'next': safe_redirect_target(request.form.get('next'), '')})
    return redirect(url)


def _open_flow(service_name: str) -> dict | None:
    try:
        flow = unseal_with(KEY, session.get(FLOW) or '')
    except SealUnavailable:
        return None
    if flow.get('service') != service_name or flow.get('user_id') != current_user.id:
        return None
    return flow


def _store(service, account: Account) -> SocialConnection:
    connection = SocialConnection.query.filter_by(user_id=current_user.id, service=service.name,
                                                  remote_id=account.remote_id).first()
    if connection is None:
        connection = SocialConnection(user_id=current_user.id, service=service.name, remote_id=account.remote_id,
                                      default_on=service.default_on)
        db.session.add(connection)
    connection.variant = account.variant
    connection.remote_name = account.remote_name[:255]
    connection.sealed_tokens = seal_tokens(account.tokens)
    with for_domain(current_user.site_domain_id):   # which app issued these tokens (C10, C11)
        kind = service.kind_of(account.variant)
        app = current(kind)
        connection.app_domain_id = app.domain_id if app is not None else None
        connection.app_client_id = bluesky.client_id() if kind == 'bluesky' else (app.client_id if app else None)
    connection.state, connection.last_error = 'ok', None
    if account.identity and identity_of_member() is None:   # A8: the identity when they have none
        connection.is_identity = True
    db.session.commit()
    if connection.is_identity:   # new, or reconnected: what they did meanwhile, on Bluesky now
        atproto_identity.caught_up(current_user.id)
    return connection


def identity_of_member() -> SocialConnection | None:
    return SocialConnection.query.filter_by(user_id=current_user.id, service='bluesky', is_identity=True).first()


@bp.route('/user/settings/connections/<service_name>/callback', methods=['GET'])
@login_required
def user_connection_callback(service_name):
    service = _service_or_404(service_name)
    flow = _open_flow(service.name)
    state = request.args.get('state', '')
    if flow is None or not secrets.compare_digest(state.encode(), flow['state'].encode()):
        session.pop(FLOW, None)
        flash(_('That sign-in did not start here, or has expired. Please try again.'), 'error')
        return redirect(url_for('user.user_connections'))
    if request.args.get('error'):
        session.pop(FLOW, None)
        flash(_('%(service)s did not connect: %(error)s', service=service.label,
                error=request.args.get('error_description') or request.args['error'])[:300], 'warning')
        return redirect(url_for('user.user_connections'))
    try:
        with for_domain(current_user.site_domain_id):
            accounts = service.finish(flow['variant'], request.args.to_dict(), _redirect_uri(service.name),
                                      flow['pending'])
    except ServiceError as failure:
        session.pop(FLOW, None)
        flash(_('%(service)s did not connect: %(error)s', service=service.label, error=failure.message), 'error')
        return redirect(url_for('user.user_connections'))
    if not accounts:
        session.pop(FLOW, None)
        flash(_('That sign-in gave access to no account %(service)s can post to.', service=service.label), 'warning')
        return redirect(url_for('user.user_connections'))
    if len(accounts) == 1:
        session.pop(FLOW, None)
        connection = _store(service, accounts[0])
        flash(_('Connected %(service)s: %(name)s', service=service.label, name=connection.remote_name))
        return redirect(flow.get('next') or url_for('user.user_connections'))
    flow['accounts'] = [vars(account) for account in accounts]
    session[FLOW] = seal_with(KEY, flow)
    return redirect(url_for('user.user_connection_choose', service_name=service.name))


@bp.route('/user/settings/connections/<service_name>/choose', methods=['GET', 'POST'])
@login_required
def user_connection_choose(service_name):
    service = _service_or_404(service_name)
    flow = _open_flow(service.name)
    if flow is None or not flow.get('accounts'):
        return redirect(url_for('user.user_connections'))
    accounts = [Account(**account) for account in flow['accounts']]
    if request.method == 'POST':
        choice = request.form.get('account', '')
        if not choice.isdigit() or int(choice) >= len(accounts):
            abort(400)
        session.pop(FLOW, None)
        connection = _store(service, accounts[int(choice)])
        flash(_('Connected %(service)s: %(name)s', service=service.label, name=connection.remote_name))
        return redirect(url_for('user.user_connections'))
    return render_template('user/connection_choose.html', title=_('Connected accounts'), service=service,
                           variant=flow['variant'], names=[account.remote_name for account in accounts])


@bp.route('/user/settings/connections/<int:connection_id>/default', methods=['POST'])
@login_required
def user_connection_default(connection_id):
    connection = _own_connection_or_404(connection_id)
    connection.default_on = request.form.get('default_on') == '1'
    db.session.commit()
    return redirect(url_for('user.user_connections'))


@bp.route('/user/settings/connections/<int:connection_id>/disconnect', methods=['POST'])
@login_required
def user_connection_disconnect(connection_id):
    connection = _own_connection_or_404(connection_id)
    name = connection.remote_name
    if sealing_available():
        disconnect(connection)
    else:   # the key is gone: the tokens cannot be opened to revoke, but the member may still leave
        db.session.delete(connection)
        db.session.commit()
    flash(_('Disconnected %(name)s.', name=name))
    return redirect(url_for('user.user_connections'))


@bp.route('/user/settings/crossposts/<int:crosspost_id>/retry', methods=['POST'])
@login_required
def user_crosspost_retry(crosspost_id):
    row = db.session.get(SocialCrosspost, crosspost_id)
    if row is None or row.post.user_id != current_user.id:
        abort(404)
    if row.state not in ('failed', 'unconfirmed') or row.connection is None:
        abort(400)
    retry(row)
    return redirect(f'/post/{row.post_id}')


# --------------------------------------------------------------------------
# a Bluesky account made here (ATProto spec §8, §9)
# --------------------------------------------------------------------------

def _own_hosted_or_404(connection_id: int) -> SocialConnection:
    connection = _own_connection_or_404(connection_id)
    if connection.variant != hosted.HOSTED or not _at_home():
        abort(404)
    return connection


@bp.route('/user/settings/connections/bluesky/create', methods=['POST'])
@login_required
def user_bluesky_create():
    """One click (A9): the account is made under the derived handle, or the member waits for a slot. A handle the
    PDS will not give brings the page back offering the next one."""
    if not _at_home() or not hosted.hosting_on(current_user.site_domain_id):
        abort(404)
    try:
        connection = hosted.request_account(current_user, request.form.get('label', '')[:64] or None)
    except hosted.HandleRefused as refused:
        flash(_('%(error)s Try @%(handle)s instead?', error=refused.message,
                handle=hosted.handle_of(current_user, refused.suggestion)), 'warning')
        return redirect(url_for('user.user_connections', label=refused.suggestion))
    except ServiceError as failure:
        flash(_('Bluesky could not be reached: %(error)s', error=failure.message), 'error')
        return redirect(url_for('user.user_connections'))
    if connection.slot_state == hosted.ACTIVE:
        flash(_('Your Bluesky account @%(handle)s is ready.', handle=connection.remote_name))
    else:
        flash(_('Every Bluesky place here is taken: you are on the waiting list, and will be told when yours is '
                'ready.'), 'info')
    return redirect(safe_redirect_target(request.form.get('next'), url_for('user.user_connections')))


@bp.route('/user/settings/connections/<int:connection_id>/turn-off', methods=['POST'])
@login_required
def user_bluesky_turn_off(connection_id):
    connection = _own_hosted_or_404(connection_id)
    if connection.slot_state != hosted.ACTIVE:
        abort(400)
    try:
        hosted.deactivate(connection)
    except ServiceError as failure:
        flash(_('Bluesky could not be reached: %(error)s', error=failure.message), 'error')
    else:
        flash(_('@%(handle)s is turned off: it keeps its name, and reads as deactivated on Bluesky.',
                handle=connection.remote_name))
    return redirect(url_for('user.user_connections'))


@bp.route('/user/settings/connections/<int:connection_id>/turn-on', methods=['POST'])
@login_required
def user_bluesky_turn_on(connection_id):
    connection = _own_hosted_or_404(connection_id)
    if connection.slot_state != hosted.DEACTIVATED:
        abort(400)
    try:
        hosted.reactivate(connection)
    except ServiceError as failure:
        flash(_('Bluesky could not be reached: %(error)s', error=failure.message), 'error')
    else:
        flash(_('@%(handle)s is on again.', handle=connection.remote_name) if connection.slot_state == hosted.ACTIVE
              else _('Every Bluesky place here is taken: you are on the waiting list.'))
    return redirect(url_for('user.user_connections'))


@bp.route('/user/settings/connections/<int:connection_id>/handle', methods=['POST'])
@login_required
def user_bluesky_handle(connection_id):
    connection = _own_hosted_or_404(connection_id)
    if connection.slot_state != hosted.ACTIVE:
        abort(400)
    try:
        handle = hosted.rename(connection, request.form.get('label', '')[:64])
    except hosted.HandleRefused as refused:
        flash(_('%(error)s Try @%(handle)s instead?', error=refused.message,
                handle=hosted.handle_of(current_user, refused.suggestion)), 'warning')
    except ServiceError as failure:
        flash(_('Bluesky could not be reached: %(error)s', error=failure.message), 'error')
    else:
        flash(_('Your Bluesky handle is now @%(handle)s.', handle=handle))
    return redirect(url_for('user.user_connections'))


@bp.route('/user/settings/connections/<int:connection_id>/app-password', methods=['POST'])
@login_required
def user_bluesky_app_password(connection_id):
    """A new app password, shown on this page once: never stored, never put in the session cookie."""
    connection = _own_hosted_or_404(connection_id)
    if connection.slot_state != hosted.ACTIVE:
        abort(400)
    try:
        password = hosted.app_password(connection)
    except ServiceError as failure:
        flash(_('Bluesky could not be reached: %(error)s', error=failure.message), 'error')
        return redirect(url_for('user.user_connections'))
    response = make_response(render_template('user/bluesky_app_password.html', title=_('App password'),
                                             user=current_user, handle=connection.remote_name, password=password))
    response.headers['Cache-Control'] = 'no-store'
    return response
