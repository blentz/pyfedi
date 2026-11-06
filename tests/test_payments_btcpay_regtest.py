"""The adapter against a real BTCPay Server on Bitcoin regtest, end to end. Skipped unless that server can be reached:

    CONTAINER_CONNECTION=remote-gpu podman-compose -f compose.btcpay-regtest.yaml up -d

then the test runner joins its network, `podman network connect pf_btcpay pyfedi_test-runner_1`. Regtest coins are worth nothing.

BTCPay won't deliver a webhook to a private address, so this reads what it stored for delivery, signs it as the docs say,
and posts it to the route. So what's proved is everything but the signature's format and the delivery itself.
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from flask import current_app, g

from app import db
from app.models import MemberSubscription, Offer, Payment, PaymentProvider, Site
from app.payments.adapters.base import ProviderError
from app.payments.adapters.btcpay_adapter import BtcpayAdapter
from app.payments.credentials import seal
from tests.factories import make_user

SERVER = 'http://btcpayserver:49392'
BITCOIND = 'http://bitcoind:43782/wallet/miner'
ADMIN = ('admin@example.com', 'regtest-regtest1')


def reachable() -> bool:
    try:
        return httpx.get(f'{SERVER}/api/v1/health', timeout=2).json().get('synchronized') is True
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not reachable(), reason='no BTCPay Server on regtest')


@pytest.fixture(scope='module', autouse=True)
def real_network_to_these_two():
    with respx.mock(assert_all_called=False) as router:
        router.route(host__in=['btcpayserver', 'bitcoind']).pass_through()
        yield


def rpc(method, *params):
    response = httpx.post(BITCOIND, auth=('regtest', 'regtest'), timeout=30,
                          json={'jsonrpc': '1.0', 'id': 'test', 'method': method, 'params': list(params)})
    assert response.json()['error'] is None, response.text
    return response.json()['result']


def wait_for(check, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = check()
        if found:
            return found
        time.sleep(2)
    raise AssertionError('timed out')


@pytest.fixture(scope='module')
def server():
    client = httpx.Client(base_url=SERVER, timeout=30)
    client.post('/api/v1/users', json={'email': ADMIN[0], 'password': ADMIN[1], 'isAdministrator': True})
    key = client.post('/api/v1/api-keys', auth=ADMIN, json={'label': 'test', 'permissions': [
        'btcpay.store.canmodifystoresettings', 'btcpay.store.cancreateinvoice', 'btcpay.store.canviewinvoices',
        'btcpay.store.webhooks.canmodifywebhooks']}).json()['apiKey']
    least = client.post('/api/v1/api-keys', auth=ADMIN, json={'label': 'least', 'permissions': [
        'btcpay.store.cancreateinvoice', 'btcpay.store.canviewinvoices']}).json()['apiKey']
    headers = {'Authorization': f'token {key}'}
    store = client.post('/api/v1/stores', headers=headers, json={'name': 'test', 'defaultCurrency': 'USD'}).json()['id']
    assert client.post(f'/api/v1/stores/{store}/payment-methods/BTC-CHAIN/wallet/generate', auth=ADMIN,
                       json={'savePrivateKeys': True}).status_code == 200
    webhook = client.post(f'/api/v1/stores/{store}/webhooks', headers=headers, json={
        'url': 'https://example.com/never-delivered',
        'authorizedEvents': {'everything': False, 'specificEvents': ['InvoiceSettled', 'InvoiceInvalid']}}).json()
    return SimpleNamespace(client=client, headers=headers, store=store, webhook=webhook, least=least)


@pytest.fixture
def env(app, api_baseline, server, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    g.admin_ids = []
    db.session.get(Site, 1).private_instance = False
    db.session.add(PaymentProvider(kind='btcpay', credentials=seal({'api_key': server.least, 'webhook_secret': server.webhook['secret']}),
                                   settings={'url': SERVER, 'onion_url': '', 'store_id': server.store}))
    plan = Offer(provider_kind='btcpay', kind='subscription', title='Prepaid', offer_group='plans', amount_minor=500, currency='usd',
                 interval='month', grants_donor=True)
    db.session.add(plan)
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), plan=plan, member=member, app=app)


def pay_on_chain(server, invoice_id):
    methods = server.client.get(f'/api/v1/stores/{server.store}/invoices/{invoice_id}/payment-methods', headers=server.headers).json()
    rpc('sendtoaddress', methods[0]['destination'], float(Decimal(methods[0]['due'])), '', '', False, False, None, 'unset', False, 2)
    rpc('generatetoaddress', 2, rpc('getnewaddress'))
    wait_for(lambda: server.client.get(f'/api/v1/stores/{server.store}/invoices/{invoice_id}',
                                       headers=server.headers).json()['status'] == 'Settled')


def stored_hook(server, invoice_id, kind):
    """What the server would have delivered."""
    base = f"/api/v1/stores/{server.store}/webhooks/{server.webhook['id']}/deliveries"

    def find():
        for delivery in server.client.get(base, headers=server.headers).json():
            sent = server.client.get(f"{base}/{delivery['id']}/request", headers=server.headers).json()
            if sent.get('invoiceId') == invoice_id and sent.get('type') == kind:
                return sent
    return wait_for(find)


def deliver(env, server, event):
    raw = json.dumps(event).encode()
    signature = 'sha256=' + hmac.new(server.webhook['secret'].encode(), raw, hashlib.sha256).hexdigest()
    return env.client.post('/payments/webhook/btcpay', data=raw, headers={'BTCPay-Sig': signature})


def test_a_key_with_the_least_permissions_is_accepted(env, server):
    BtcpayAdapter().check_credentials({'url': SERVER, 'store_id': server.store, 'api_key': server.least})


def test_a_bad_key_is_refused(env, server):
    with pytest.raises(ProviderError):
        BtcpayAdapter().check_credentials({'url': SERVER, 'store_id': server.store, 'api_key': 'nope'})


def test_paying_an_invoice_on_chain_buys_a_period(env, server):
    with env.app.test_request_context('/'):
        address = BtcpayAdapter().checkout(env.plan, env.member, 'https://example.com/back', 'https://example.com/no')
    invoice_id = address.rsplit('/', 1)[1]
    assert address == f'{SERVER}/i/{invoice_id}'
    assert server.client.get(f'/i/{invoice_id}').status_code == 200   # a page a browser can open

    pay_on_chain(server, invoice_id)
    event = stored_hook(server, invoice_id, 'InvoiceSettled')
    assert deliver(env, server, event).status_code == 200

    payment = Payment.query.filter_by(provider_kind='btcpay', provider_reference=invoice_id).one()
    assert (payment.status, payment.amount_minor, payment.currency, payment.user_id) == ('paid', 500, 'usd', env.member.id)
    row = MemberSubscription.query.filter_by(provider_reference=invoice_id).one()
    assert row.status == 'active' and row.current_period_end is not None

    assert deliver(env, server, {**event, 'deliveryId': 'another', 'isRedelivery': True}).status_code == 200
    assert Payment.query.filter_by(provider_reference=invoice_id).count() == 1
