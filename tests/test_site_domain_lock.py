"""A domain's federation setting and the domain itself are locked once it has accounts or communities."""
import pytest

from app import db
from app.local_only import domain_has_owners
from app.models import Community, Instance, SiteDomain
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


def _row(domain='shop.example', **kw):
    row = SiteDomain(domain=domain, scheme='https', federates=True, registration='anyone', is_default=False, **kw)
    db.session.add(row)
    db.session.commit()
    return row


def _owned_user(row):
    user = make_user(Instance.query.filter_by(domain='test.piefed.local').first()
                     or make_instance('test.piefed.local', software='piefed'), 'owner', local=True)
    user.site_domain_id = row.id
    db.session.commit()


def test_empty_row_may_change_federates():
    row = _row()
    row.federates = False
    db.session.commit()
    assert db.session.get(SiteDomain, row.id).federates is False
    assert not domain_has_owners(row.id)


def test_row_with_account_refuses_change():
    row = _row()
    _owned_user(row)
    with pytest.raises(ValueError):
        row.federates = False
    row.federates = True  # unchanged value is fine
    assert domain_has_owners(row.id)


def test_row_with_community_refuses_change():
    row = _row()
    community = Community(name='shopcomm', title='t', site_domain_id=row.id)
    db.session.add(community)
    db.session.commit()
    with pytest.raises(ValueError):
        row.federates = False


def test_default_row_owns_unstamped_local_users():
    row = _row('def.example', )
    row.is_default = True
    db.session.commit()
    make_user(make_instance('test.piefed.local', software='piefed'), 'legacy', local=True)
    assert domain_has_owners(row.id)
    with pytest.raises(ValueError):
        row.federates = False


def _admin():
    instance = Instance.query.filter_by(domain='test.piefed.local').first() \
        or make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)  # user 1 passes every check
    user = make_user(instance, 'lockadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    return user


def test_delete_with_owners_keeps_row(app):
    row = _row()
    admin = _admin()
    _owned_user(row)
    client = app.test_client()
    login(client, admin)
    client.post(f'/admin/domains/{row.id}/delete', data={'csrf_token': csrf(app, client)})
    assert db.session.get(SiteDomain, row.id) is not None


def test_delete_empty_row(app):
    row = _row()
    rid = row.id
    client = app.test_client()
    login(client, _admin())
    client.post(f'/admin/domains/{rid}/delete', data={'csrf_token': csrf(app, client)})
    db.session.expire_all()
    assert db.session.get(SiteDomain, rid) is None
