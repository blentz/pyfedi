"""Admin "Domains" section (spec 2026-10-17 §3.1, §3.2)."""
import pytest
from flask import current_app

from app import db
from app.models import Instance, InstanceBlock, Site, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


def _admin(name='domainadmin'):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)  # user 1 passes every check
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    return user


def _default_row():
    row = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True,
                     registration='anyone', is_default=True)
    db.session.add(row)
    db.session.commit()
    return row


def _form(token, **overrides):
    data = {'domain': 'abc.onion', 'scheme': 'http', 'federates': '', 'registration': 'staff',
            'csrf_token': token, 'submit': 'Add'}
    data.update(overrides)
    return data


def _domains():
    return {r.domain for r in db.session.query(SiteDomain).all()}


def test_non_admin_forbidden(app):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    staff = make_user(instance, 'staffer', local=True)
    staff.verified = True
    db.session.commit()
    grant_permission(staff, 'approve registrations')
    client = app.test_client()
    login(client, staff)
    response = client.get('/admin/domains')
    assert response.status_code == 302 and 'permission_denied' in response.headers['Location']


def test_add_domain(app):
    client = app.test_client()
    login(client, _admin())
    response = client.post('/admin/domains', data=_form(csrf(app, client), domain='ABC.onion'))
    assert response.status_code == 302
    row = db.session.query(SiteDomain).filter_by(domain='abc.onion').one()
    assert (row.is_onion, row.is_default, row.scheme, row.federates, row.registration) == \
           (True, False, 'http', False, 'staff')
    assert domain_for_host('abc.onion') is not None


def test_the_form_adds_a_second_federating_domain_with_its_keys_and_instance_row(app):
    """Ship dark (C3): the form takes federates=True for a non-default domain. The row gets its own key pair
    (never the default's) and its own instance row."""
    default = _default_row()
    client = app.test_client()
    login(client, _admin())
    response = client.post('/admin/domains', data=_form(csrf(app, client), domain='shop.example',
                                                        scheme='https', federates='y', registration='anyone'))
    assert response.status_code == 302
    row = db.session.query(SiteDomain).filter_by(domain='shop.example').one()
    assert (row.federates, row.is_default) == (True, False)
    assert row.public_key.startswith('-----BEGIN PUBLIC KEY-----')
    assert row.private_key.startswith('-----BEGIN PRIVATE KEY-----')
    assert row.public_key != default.public_key
    assert db.session.get(Instance, row.instance_id).domain == 'shop.example'
    assert row.instance_id != default.instance_id


def test_the_federation_lock_still_refuses_flipping_a_form_made_domain_with_owners(app):
    _default_row()
    client = app.test_client()
    login(client, _admin())
    client.post('/admin/domains', data=_form(csrf(app, client), domain='shop.example', federates='y'))
    row = db.session.query(SiteDomain).filter_by(domain='shop.example').one()
    make_user(db.session.get(Instance, row.instance_id), 'shopper', local=True).site_domain_id = row.id
    db.session.commit()
    with pytest.raises(ValueError):
        row.federates = False


def test_the_federates_box_starts_unticked(app):
    client = app.test_client()
    login(client, _admin())
    page = client.get('/admin/domains').get_data(as_text=True)
    box = page[page.index('id="federates"') - 200:page.index('id="federates"') + 200]
    assert 'checked' not in box


def test_add_seeds_default_when_table_empty(app):
    client = app.test_client()
    login(client, _admin())
    client.post('/admin/domains', data=_form(csrf(app, client)))
    default = db.session.query(SiteDomain).filter_by(is_default=True).one()
    assert default.domain == current_app.config['SERVER_NAME']


def test_add_leaves_existing_default(app):
    _default_row()
    client = app.test_client()
    login(client, _admin())
    client.post('/admin/domains', data=_form(csrf(app, client), federates=''))
    assert db.session.query(SiteDomain).filter_by(is_default=True).count() == 1
    assert db.session.query(SiteDomain).filter_by(domain='abc.onion').one().federates is False


@pytest.mark.parametrize('bad', ['https://x.y', 'x y', '', 'x.y:80', 'a..b', 'x_y.z', 'é.com'])
def test_invalid_domain_rejected(app, bad):
    client = app.test_client()
    login(client, _admin())
    response = client.post('/admin/domains', data=_form(csrf(app, client), domain=bad))
    assert response.status_code == 200
    assert db.session.query(SiteDomain).count() == 0


def test_duplicate_rejected(app):
    _default_row()
    client = app.test_client()
    login(client, _admin())
    token = csrf(app, client)
    client.post('/admin/domains', data=_form(token))
    response = client.post('/admin/domains', data=_form(token, domain='Abc.Onion.'))
    assert response.status_code == 200
    assert b'already' in response.data
    assert db.session.query(SiteDomain).filter_by(domain='abc.onion').count() == 1


def test_default_radio_locked(app):
    _default_row()
    other = SiteDomain(domain='abc.onion', scheme='http')
    db.session.add(other)
    db.session.commit()
    client = app.test_client()
    login(client, _admin())
    token = csrf(app, client)
    html = client.get('/admin/domains').get_data(as_text=True)
    assert 'type="radio"' in html and 'disabled' in html
    assert 'changing the default domain is not supported yet' in html
    # forged POST naming another row as default
    client.post('/admin/domains', data=_form(token, domain='def.onion', is_default='y',
                                             default=str(other.id)))
    db.session.expire_all()
    assert db.session.query(SiteDomain).filter_by(is_default=True).one().domain == \
           current_app.config['SERVER_NAME']
    assert db.session.query(SiteDomain).filter_by(domain='def.onion').one().is_default is False


def test_cannot_delete_default(app):
    default = _default_row()
    client = app.test_client()
    login(client, _admin())
    client.post(f'/admin/domains/{default.id}/delete', data={'csrf_token': csrf(app, client)})
    assert default.domain in _domains()


def test_delete_non_default_and_missing(app):
    _default_row()
    other = SiteDomain(domain='abc.onion', scheme='http')
    db.session.add(other)
    db.session.commit()
    client = app.test_client()
    login(client, _admin())
    token = csrf(app, client)
    assert client.post(f'/admin/domains/{other.id}/delete', data={'csrf_token': token}).status_code == 302
    assert 'abc.onion' not in _domains()
    assert domain_for_host('abc.onion') is None
    assert client.post('/admin/domains/99999/delete', data={'csrf_token': token}).status_code == 404


def test_empty_table_shows_implicit_default(app):
    client = app.test_client()
    login(client, _admin())
    html = client.get('/admin/domains').get_data(as_text=True)
    assert current_app.config['SERVER_NAME'] in html


def test_page_says_later_release_and_dns(app):
    _default_row()
    client = app.test_client()
    login(client, _admin())
    html = client.get('/admin/domains').get_data(as_text=True)
    assert html.count('takes effect in a later release') >= 1
    assert 'deploy/DEPLOY_GUIDE.md' in html
    assert 'DNS' in html and 'TLS' in html and 'Tor' in html


# --- a deleted domain takes its instance row with it (plan C2 final wave) ---

def _onion_row():
    _default_row()
    row = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    return row


def _delete(app, row):
    client = app.test_client()
    login(client, _admin())
    client.post(f'/admin/domains/{row.id}/delete', data={'csrf_token': csrf(app, client)})
    return client


def test_deleting_a_domain_deletes_its_instance_row_so_no_peer_list_names_it(app):
    row = _onion_row()
    instance_id = row.instance_id
    peer = make_instance('peer.example', software='piefed')
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    client = _delete(app, row)
    assert 'abc.onion' not in _domains()
    assert db.session.get(Instance, instance_id) is None
    assert Instance.query.filter(db.func.lower(Instance.domain) == 'abc.onion').first() is None
    for path in ('/instances', '/api/v3/federated_instances'):
        body = client.get(path).get_data(as_text=True)
        assert peer.domain in body and 'abc.onion' not in body, path


@pytest.mark.parametrize('reference', ['user', 'block'])
def test_a_domain_whose_instance_row_is_referenced_is_not_deleted(app, reference):
    row = _onion_row()
    instance_id = row.instance_id
    peer = make_instance('peer.example', software='piefed')
    if reference == 'user':   # an account on the row that the domain does not own
        make_user(db.session.get(Instance, instance_id), 'stray')
    else:   # a hell.cloud account that blocked the onion's row
        blocker = make_user(make_instance('test.piefed.local', software='piefed'), 'blocker', local=True)
        db.session.add(InstanceBlock(user_id=blocker.id, instance_id=instance_id))
    db.session.commit()
    client = _delete(app, row)
    assert 'abc.onion' in _domains()
    assert db.session.get(Instance, instance_id) is not None
    html = client.get('/admin/domains').get_data(as_text=True)
    assert 'This domain still has content, blocks or other records on its instance row' in html
