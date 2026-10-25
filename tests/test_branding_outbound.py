"""Emails, the PWA manifest and what other servers and clients read name a domain by its own branding (project 4,
Task 3; spec §3.3).

- Mail is named by the recipient account's home domain, or the first domain's for mail to no account, with the
  pages' fallback: a blank field is Site's. A Celery task or a cron job has no request and no `g.site`, so mail reads
  `branding_for`, never `g.site`.
- /manifest.json is per request host, with the pages' fallback.
- NodeInfo, the Lemmy site document, the ActivityPub Application document and the alpha API's site documents publish
  a sister domain's own value where it set one, and exactly today's otherwise. The first domain's do not change by a
  byte (tests/test_c3_golden_documents.py), and a sister never names the first domain.

shop.example federates beside the first domain (`golden_site`, `shop_rows`); Site row 1 is named as the first
domain's host (`site_named_hell`), so a document that copies the site's name onto the shop shows in its body."""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app, g, has_request_context, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import cache, cli, db
from app.admin.util import send_newsletter
from app.constants import NOTIF_POST
from app.email import (mail_name, send_password_reset_email, send_registration_approved_email,
                       send_topic_suggestion, send_verification_email)
from app.models import DomainBranding, Notification, Site, User
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import utcnow
from tests.test_c3_golden_documents import AP, DISCOVERY_PATHS, GOLDEN_PATHS, SHOP, golden_copy, golden_site  # noqa: F401
from tests.test_per_domain_actor_documents import SHOP_BASE, shop_rows, site_named_hell  # noqa: F401  (fixtures)
from tests.test_per_domain_discovery import alpha_api, alpha_get  # noqa: F401  (a fixture)
from tests.two_domains import HELL, two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('golden_site', 'shop_rows', 'site_named_hell')

SHOP_OWN = dict(name='Shop Branded', description='The shop tagline', logo='/static/media/shop_100.png',
                logo_180='/static/media/shop_180.png', logo_152='/static/media/shop_152.png',
                logo_32='/static/media/shop_32.png', logo_16='/static/media/shop_16.png',
                sidebar='The shop *sidebar*', sidebar_html='<p>The shop <em>sidebar</em></p>',
                about='The shop about', about_html='<p>The shop about</p>')
BLANK = dict.fromkeys(SHOP_OWN, '')
DEFAULT_ICON = '/static/images/piefed_logo_icon_t_75.png'


def brand(domain, **values):
    db.session.add(DomainBranding(site_domain_id=domain.id, **values))
    db.session.commit()
    clear_domain_cache()
    cache.clear()


def get(app, path, host=SHOP, headers=None):
    response = app.test_client().get(path, base_url=f'https://{host}', headers=headers or {})
    assert response.status_code == 200, (host, path, response.status_code)
    return response


# ---------------------------------------------------------------------------------------------------------------------
# Emails

@pytest.fixture
def mail_site(app, golden_site, shop_rows):
    """Site row 1 with a logo and a contact address; `g` with no `site`, as a Celery task's is."""
    site = db.session.get(Site, 1)
    site.logo = '/static/media/site_100.png'
    site.contact_email = 'admin@example.org'
    for user in db.session.query(User):
        user.verification_token = f'token{user.id}'
    db.session.commit()
    g.pop('site', None)
    yield site


def celery_style():
    assert not has_request_context()
    assert 'site' not in g


SINGLE_ACCOUNT_MAILS = [pytest.param(send_password_reset_email, id='password reset'),
                        pytest.param(send_verification_email, id='verification'),
                        pytest.param(send_registration_approved_email, id='approval')]


@pytest.mark.usefixtures('mail_site')
@pytest.mark.parametrize('send_mail', SINGLE_ACCOUNT_MAILS)
def test_email_sender_names_recipients_home_domain(app, shop_rows, send_mail):
    """Review Focus 4: the From name and the logo are the recipient's home domain's, in a call with no request
    context and no g.site; the first domain's account is named by Site."""
    shop, shop_user, _ = shop_rows
    hell_user = db.session.query(User).filter_by(site_domain_id=domain_for_host(HELL).id).one()
    brand(shop, **SHOP_OWN)
    mail_from = current_app.config['MAIL_FROM']
    sent = {}
    for user in (shop_user, hell_user):
        celery_style()
        with patch('app.email.send_email') as send:
            send_mail(user)
        sent[user.id] = send.call_args.kwargs
    assert sent[shop_user.id]['sender'] == f'Shop Branded <{mail_from}>'
    assert '/static/media/shop_100.png' in sent[shop_user.id]['html_body']
    assert sent[hell_user.id]['sender'] == f'{HELL} <{mail_from}>'
    assert '/static/media/site_100.png' in sent[hell_user.id]['html_body']


@pytest.mark.usefixtures('mail_site')
def test_a_sister_with_no_own_name_sends_as_site(app, shop_rows):
    """The pages' fallback: a blank field is Site's."""
    shop, shop_user, _ = shop_rows
    brand(shop, **BLANK)
    celery_style()
    with patch('app.email.send_email') as send:
        send_verification_email(shop_user)
    assert send.call_args.kwargs['sender'] == f'{HELL} <{current_app.config["MAIL_FROM"]}>'
    assert '/static/media/site_100.png' in send.call_args.kwargs['html_body']


@pytest.mark.usefixtures('mail_site')
def test_an_approval_with_no_from_address_sends_as_the_contact_with_no_g_site(app, shop_rows, monkeypatch):
    shop, shop_user, _ = shop_rows
    brand(shop, **SHOP_OWN)
    monkeypatch.setitem(current_app.config, 'MAIL_FROM', '')
    celery_style()
    with patch('app.email.send_email') as send:
        send_registration_approved_email(shop_user)
    assert send.call_args.kwargs['sender'] == 'Shop Branded <admin@example.org>'
    assert send.call_args.kwargs['reply_to'] == 'admin@example.org'


@pytest.mark.usefixtures('mail_site')
def test_mail_to_no_account_is_the_first_domains(app, shop_rows):
    """A topic suggestion goes to the admins, not to an account: the first domain's name and logo."""
    shop, shop_user, community = shop_rows
    brand(shop, **SHOP_OWN)
    assert mail_name() == HELL
    assert mail_name(shop_user) == 'Shop Branded'
    celery_style()
    with patch('app.email.send_email') as send:
        send_topic_suggestion([community], shop_user, 'admin@example.org', 'A subject', 'Gardening')
    kwargs = send.call_args.kwargs
    assert kwargs['sender'] == f'{HELL} <{current_app.config["MAIL_FROM"]}>'
    assert kwargs['text_body'].startswith(f'Hi {HELL} Admin!')
    assert '/static/media/site_100.png' in kwargs['html_body'] and 'shop_100' not in kwargs['html_body']


def test_a_topic_suggestion_is_titled_by_the_first_domain(app, shop_rows):
    """The subject names the first domain, whichever domain the member suggests it on."""
    shop, shop_user, _ = shop_rows
    brand(shop, **SHOP_OWN)
    shop_user.reputation = 100
    shop_user.created = utcnow() - timedelta(days=30)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction(base_url=SHOP_BASE) as session:
        session['_user_id'] = str(shop_user.id)
        session['_fresh'] = True
    with app.test_request_context(base_url=SHOP_BASE):
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction(base_url=SHOP_BASE) as session:
        session['csrf_token'] = raw
    with patch('app.topic.routes.send_topic_suggestion') as send:
        response = client.post('/topics/new', base_url=SHOP_BASE,
                               data={'topic_name': 'Gardening', 'communities_for_topic': 'a', 'csrf_token': token})
    assert response.status_code == 302
    assert send.call_args.args[3] == f'New topic suggestion from {HELL}'


@pytest.mark.usefixtures('mail_site')
def test_a_newsletter_is_from_each_readers_home_domain(app, shop_rows):
    shop, shop_user, _ = shop_rows
    hell_user = db.session.query(User).filter_by(site_domain_id=domain_for_host(HELL).id).one()
    for user in (shop_user, hell_user):
        user.newsletter = True
    db.session.commit()
    brand(shop, **SHOP_OWN)
    form = type('Form', (), {})()
    form.subject = type('Field', (), {'data': 'News'})()
    form.body_text = type('Field', (), {'data': 'hello'})()
    form.body_html = type('Field', (), {'data': '<p>hello</p>'})()
    form.test = type('Field', (), {'data': False})()
    with app.test_request_context(base_url=f'https://{HELL}'):
        app.preprocess_request()
        with patch('app.admin.util.send_email') as send:
            send_newsletter(form)
    by_recipient = {call.kwargs['recipients'][0]: call.kwargs for call in send.call_args_list}
    mail_from = current_app.config['MAIL_FROM']
    assert by_recipient[shop_user.email]['sender'] == f'Shop Branded <{mail_from}>'
    assert '/static/media/shop_100.png' in by_recipient[shop_user.email]['html_body']
    assert by_recipient[hell_user.email]['sender'] == f'{HELL} <{mail_from}>'
    assert '/static/media/site_100.png' in by_recipient[hell_user.email]['html_body']


@pytest.mark.usefixtures('mail_site')
def test_the_missed_notifications_mail_is_from_each_accounts_home_domain(app, shop_rows):
    """A cron job: no request, no g.site."""
    shop, shop_user, _ = shop_rows
    hell_user = db.session.query(User).filter_by(site_domain_id=domain_for_host(HELL).id).one()
    for user in (shop_user, hell_user):
        user.email_unread = True
        user.email_unread_sent = False
        user.last_seen = utcnow() - timedelta(days=2)
        db.session.add(Notification(user_id=user.id, title='a notification', url='/x', notif_type=NOTIF_POST,
                                    created_at=utcnow() - timedelta(hours=1)))
    db.session.commit()
    brand(shop, **SHOP_OWN)
    sent = {}
    celery_style()
    with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: k})):
        cli.register(current_app)
        result = current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    assert result.exception is None, result.exception
    mail_from = current_app.config['MAIL_FROM']
    assert sent[shop_user.email]['sender'] == f'Shop Branded <{mail_from}>'
    assert '/static/media/shop_100.png' in sent[shop_user.email]['html_body']
    assert sent[hell_user.email]['sender'] == f'{HELL} <{mail_from}>'


@pytest.mark.parametrize('logged_in', [True, False])
def test_the_test_email_is_from_the_callers_home_domain(app, shop_rows, monkeypatch, logged_in):
    shop, shop_user, _ = shop_rows
    brand(shop, **SHOP_OWN)
    monkeypatch.setattr(current_app, 'debug', True)
    client = app.test_client()
    if logged_in:
        with client.session_transaction(base_url=SHOP_BASE) as session:
            session['_user_id'] = str(shop_user.id)
            session['_fresh'] = True
    with patch('app.main.routes.send_email') as send:
        client.get('/test_email?email=x@example.org', base_url=SHOP_BASE)
    name = 'Shop Branded' if logged_in else HELL
    assert send.call_args.kwargs['sender'] == f'{name} <{current_app.config["MAIL_FROM"]}>'


# ---------------------------------------------------------------------------------------------------------------------
# The PWA manifest

def manifest(app, host):
    return get(app, '/manifest.json', host=host).get_json()


def icons(document) -> dict:
    return {icon['sizes']: icon['src'] for icon in document['icons']}


def test_manifest_is_per_host(app, shop_rows):
    shop = shop_rows[0]
    hell_before = manifest(app, HELL)
    shop_before = manifest(app, SHOP)
    assert (shop_before['name'], shop_before['description']) == (HELL, 'Fixed for the golden copies')   # as today
    brand(shop, **SHOP_OWN)
    shop_document = manifest(app, SHOP)
    assert shop_document['id'] == SHOP_BASE
    assert (shop_document['name'], shop_document['description']) == ('Shop Branded', 'The shop tagline')
    assert icons(shop_document) == {'192x192': '/static/media/shop_180.png', '512x512': '/static/media/shop_180.png'}
    hell_document = manifest(app, HELL)
    assert hell_document == hell_before
    assert (hell_document['name'], hell_document['description']) == (HELL, 'Fixed for the golden copies')
    assert icons(hell_document) == {'192x192': '/static/images/piefed_logo_icon_t_192.png',
                                    '512x512': '/static/images/piefed_logo_icon_t_512.png'}


def test_a_sister_with_only_an_svg_logo_uses_it_as_its_manifest_icon(app, shop_rows):
    """An SVG upload stores no sized PNGs (app/admin/routes.py): the logo itself is the icon."""
    brand(shop_rows[0], logo='/static/media/shop.svg')
    assert icons(manifest(app, SHOP)) == {'192x192': '/static/media/shop.svg', '512x512': '/static/media/shop.svg'}


def test_a_sister_with_no_own_values_has_todays_manifest(app, shop_rows):
    before = manifest(app, SHOP)
    brand(shop_rows[0], **BLANK)
    assert manifest(app, SHOP) == before
    assert icons(before) == {'192x192': '/static/images/piefed_logo_icon_t_192.png',
                             '512x512': '/static/images/piefed_logo_icon_t_512.png'}


# ---------------------------------------------------------------------------------------------------------------------
# What other servers and clients read

FEDERATED = ['/api/v1/instance', '/api/v3/site', '/nodeinfo/2.0', '/nodeinfo/2.1']
ALPHA = ['/api/alpha/site', '/api/alpha/site/instance_chooser']


def documents(app) -> dict:
    """Every document a sister publishes about itself, as served on the shop."""
    bodies = {path: get(app, path).data for path in FEDERATED}
    bodies['/'] = get(app, '/', headers=AP).data
    bodies.update({path: alpha_get(app, path, SHOP).data for path in ALPHA})
    return bodies


@pytest.mark.usefixtures('alpha_api')
def test_sister_with_own_name_publishes_it_in_nodeinfo_actor_api(app, shop_rows):
    brand(shop_rows[0], **SHOP_OWN)
    bodies = documents(app)
    for path, body in bodies.items():
        assert HELL.encode() not in body, path   # a sister never names the first domain
    assert json.loads(bodies['/api/v1/instance'])['title'] == 'Shop Branded'

    lemmy = json.loads(bodies['/api/v3/site'])['site_view']['site']
    assert (lemmy['name'], lemmy['description'], lemmy['sidebar']) == \
        ('Shop Branded', 'The shop tagline', 'The shop *sidebar*')
    assert lemmy['icon'] == f'{SHOP_BASE}/static/media/shop_100.png'

    application = json.loads(bodies['/'])
    assert application['summary'] == 'Shop Branded - The shop tagline'
    assert application['icon']['url'] == f'{SHOP_BASE}/static/media/shop_100.png'

    alpha = json.loads(bodies['/api/alpha/site'])['site']
    assert (alpha['name'], alpha['description']) == ('Shop Branded', 'The shop tagline')
    assert (alpha['sidebar_md'], alpha['sidebar']) == ('The shop *sidebar*', '<p>The shop <em>sidebar</em></p>')
    assert alpha['icon'] == f'{SHOP_BASE}/static/media/shop_100.png'

    chooser = json.loads(bodies['/api/alpha/site/instance_chooser'])
    assert (chooser['name'], chooser['description'], chooser['about'], chooser['sidebar']) == \
        ('Shop Branded', 'The shop tagline', '<p>The shop about</p>', '<p>The shop <em>sidebar</em></p>')
    assert chooser['logo_url'] == f'{SHOP_BASE}/static/media/shop_100.png'


@pytest.mark.usefixtures('alpha_api')
def test_a_sister_names_itself_by_its_own_name_alone(app, shop_rows):
    """Each field is its own: a name with nothing else set changes the name only."""
    site = db.session.get(Site, 1)
    site.sidebar, site.sidebar_html = 'The site sidebar', '<p>The site sidebar</p>'
    db.session.commit()
    before = {path: json.loads(body) for path, body in documents(app).items()}
    brand(shop_rows[0], name='Shop Branded')
    after = {path: json.loads(body) for path, body in documents(app).items()}
    before['/api/v1/instance']['title'] = 'Shop Branded'
    before['/api/v3/site']['site_view']['site']['name'] = 'Shop Branded'
    before['/']['summary'] = 'Shop Branded - Fixed for the golden copies'
    before['/api/alpha/site']['site']['name'] = 'Shop Branded'
    before['/api/alpha/site/instance_chooser']['name'] = 'Shop Branded'
    assert after == before


@pytest.mark.usefixtures('alpha_api')
@pytest.mark.parametrize('row', [None, BLANK, dict.fromkeys(SHOP_OWN)], ids=['no row', 'blank', 'null'])
def test_sister_without_own_values_publishes_exactly_today(app, shop_rows, row):
    """Today's documents are the ones tests/test_per_domain_discovery.py pins: the shop's own domain as its name, the
    site's description and sidebar, PieFed's icon. A row with every field blank or NULL changes no byte of them."""
    site = db.session.get(Site, 1)
    site.sidebar, site.sidebar_html = 'The site sidebar', '<p>The site sidebar</p>'
    db.session.commit()
    before = documents(app)
    if row is not None:
        brand(shop_rows[0], **row)
    assert documents(app) == before
    assert json.loads(before['/api/v1/instance'])['title'] == SHOP
    lemmy = json.loads(before['/api/v3/site'])['site_view']['site']
    assert (lemmy['name'], lemmy['description'], lemmy['sidebar']) == \
        (SHOP, 'Fixed for the golden copies', 'The site sidebar')
    assert lemmy['icon'] == f'{SHOP_BASE}{DEFAULT_ICON}'
    alpha = json.loads(before['/api/alpha/site'])['site']
    assert (alpha['name'], alpha['sidebar'], alpha['icon']) == \
        (SHOP, '<p>The site sidebar</p>', f'{SHOP_BASE}{DEFAULT_ICON}')


FIRST_DOMAIN = [(name, path, AP) for name, path in sorted(GOLDEN_PATHS.items())] + \
    [(name, path, {}) for name, path in sorted(DISCOVERY_PATHS.items())]   # as tests/test_c3_golden_documents.py asks


@pytest.mark.parametrize('name, path, headers', FIRST_DOMAIN)
def test_first_domain_documents_unchanged(app, two_domains, shop_rows, name, path, headers):
    """The first domain's documents stay their golden copies beside a branded sister, and even with a branding row of
    its own, which nothing reads: its branding is Site's."""
    db.session.get(Site, 1).name = 'Golden Site'   # as the golden copies were captured
    db.session.commit()
    brand(shop_rows[0], **SHOP_OWN)
    brand(two_domains.domains[0], **SHOP_OWN)
    response = app.test_client().get(path, headers=headers, base_url=f'https://{HELL}')
    assert response.status_code == 200
    assert response.data == golden_copy(name)
