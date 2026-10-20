"""Mail and other out-of-request messages to an account link to that account's home domain (per-domain identity
spec §3.2), whichever storefront the request that triggered the send was on. This replaces ruling I1: third-party
mail no longer always names the default domain."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app, g, has_request_context, url_for

from app import cache, cli, db
from app.admin.util import send_newsletter
from app.auth.util import send_email_verification
from app.constants import NOTIF_POST
from app.email import send_password_reset_email, send_registration_approved_email, send_verification_email
from app.models import Notification, Site, SiteDomain
from app.storefront import (clear_domain_cache, default_storefront, home_storefront, storefront_memo_name,
                            storefront_url)
from app.utils import utcnow
from tests.factories import make_instance, make_user

ONION = 'abc.onion'
CANONICAL = 'test.piefed.local'

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def accounts(app, db_session):
    """An onion account and a clearnet one, on a site that serves both domains."""
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    onion_row = SiteDomain(domain=ONION, scheme='http', federates=False)
    db.session.add(onion_row)
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    instance = make_instance(CANONICAL, software='piefed')
    onion_user = make_user(instance, 'onionite', local=True)
    onion_user.site_domain_id = onion_row.id
    clear_user = make_user(instance, 'clearnetter', local=True)
    for user in (onion_user, clear_user):
        user.verification_token = f'tok{user.id}'
        user.newsletter = True
    db.session.commit()
    g.site = db.session.get(Site, 1)
    yield onion_user, clear_user
    clear_domain_cache()


def on_request(app, host):
    """A request context addressed to `host`, with the storefront resolved the way a real request does."""
    context = app.test_request_context(base_url=f'http://{host}' if host == ONION else f'https://{host}')
    context.push()
    app.preprocess_request()
    g.site = db.session.get(Site, 1)
    return context


def sent_bodies(send):
    kwargs = send.call_args.kwargs
    return kwargs['text_body'] + kwargs['html_body']


MAILS = [
    pytest.param(send_registration_approved_email, 'app.email.send_email', id='approval'),
    pytest.param(send_password_reset_email, 'app.email.send_email', id='password reset'),
    pytest.param(send_verification_email, 'app.email.send_email', id='verification'),
    pytest.param(send_email_verification, 'app.email.send_email', id='verification from sign-up'),
]


@pytest.mark.parametrize('send_mail, target', MAILS)
def test_mail_to_an_onion_account_links_to_the_onion(app, accounts, send_mail, target):
    onion_user, clear_user = accounts
    context = on_request(app, CANONICAL)   # a clearnet admin triggered it
    try:
        with patch(target) as send:
            send_mail(onion_user)
    finally:
        context.pop()
    assert f'http://{ONION}/' in sent_bodies(send)
    assert f'https://{CANONICAL}' not in sent_bodies(send)


@pytest.mark.parametrize('send_mail, target', MAILS)
def test_mail_to_a_clearnet_account_from_an_onion_request_links_to_the_clearnet(app, accounts, send_mail, target):
    onion_user, clear_user = accounts
    context = on_request(app, ONION)
    try:
        with patch(target) as send:
            send_mail(clear_user)
    finally:
        context.pop()
    assert f'https://{CANONICAL}/' in sent_bodies(send)
    assert ONION not in sent_bodies(send)


def test_the_requests_own_storefront_comes_back_after_the_mail(app, accounts):
    onion_user, clear_user = accounts
    context = on_request(app, ONION)
    try:
        with patch('app.email.send_email'):
            send_registration_approved_email(clear_user)
        assert g.storefront.domain == ONION
        assert storefront_url() == f'http://{ONION}'
    finally:
        context.pop()


def test_a_newsletter_is_rendered_for_each_recipient(app, accounts):
    onion_user, clear_user = accounts
    form = type('Form', (), {})()
    form.subject = type('Field', (), {'data': 'News'})()
    form.body_text = type('Field', (), {'data': 'hello'})()
    form.body_html = type('Field', (), {'data': '<p>hello</p>'})()
    form.test = type('Field', (), {'data': False})()
    context = on_request(app, ONION)
    try:
        with patch('app.admin.util.send_email') as send:
            send_newsletter(form)
    finally:
        context.pop()
    by_recipient = {call.kwargs['recipients'][0]: call.kwargs['text_body'] + call.kwargs['html_body']
                    for call in send.call_args_list}
    assert f'http://{ONION}/' in by_recipient[onion_user.email]
    assert CANONICAL not in by_recipient[onion_user.email]
    assert f'https://{CANONICAL}/' in by_recipient[clear_user.email]
    assert ONION not in by_recipient[clear_user.email]


def test_home_storefront_outside_a_request_names_the_home_domain(app, accounts):
    onion_user, clear_user = accounts
    assert not has_request_context()
    with home_storefront(onion_user):
        assert url_for('main.index', _external=True) == f'http://{ONION}/home'
        assert storefront_url() == f'http://{ONION}'
    assert url_for('main.index', _external=True).startswith(f'https://{CANONICAL}')
    with home_storefront(clear_user):
        assert url_for('main.index', _external=True).startswith(f'https://{CANONICAL}')


def test_what_is_memoized_per_storefront_follows_the_pinned_domain(app, accounts):
    onion_user, clear_user = accounts
    context = on_request(app, CANONICAL)
    try:
        with home_storefront(onion_user):
            assert storefront_memo_name('f') == f'f@{ONION}'
        assert storefront_memo_name('f') == f'f@{CANONICAL}'
        context2 = on_request(app, ONION)
        try:
            with default_storefront():
                assert storefront_memo_name('f') == f'f@{CANONICAL}'
        finally:
            context2.pop()
    finally:
        context.pop()


def test_the_background_email_of_an_onion_account_links_to_the_onion(app, accounts):
    onion_user, clear_user = accounts
    for user in (onion_user, clear_user):
        user.email_unread = True
        user.email_unread_sent = False
        user.last_seen = utcnow() - timedelta(days=2)
        db.session.add(Notification(user_id=user.id, title='a notification', url='/x', notif_type=NOTIF_POST,
                                    created_at=utcnow() - timedelta(hours=1)))
    db.session.commit()
    sent = {}
    with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: k['html_body']})):
        cli.register(current_app)
        result = current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    assert result.exception is None, result.exception
    assert f'http://{ONION}/' in sent[onion_user.email]
    assert CANONICAL not in sent[onion_user.email]
    assert f'https://{CANONICAL}/' in sent[clear_user.email]
    assert ONION not in sent[clear_user.email]
