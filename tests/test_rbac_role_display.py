"""RBAC build step 6: a profile names the roles its owner holds publicly, not Staff by name.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, Public display of roles: the profile badge and the user
preview show Admin plus every role with `listed_publicly`, by the role's own name. The seeded Staff role is listed,
so nothing visible changes until an admin edits it.
"""
from datetime import timedelta

import pytest

from app import db
from app.models import Role, role_grant, utcnow
from tests.factories import grant_role, make_instance, make_user, staff_role

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def people(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    holder = make_user(instance, 'holder', local=True)
    holder.verified = True
    db.session.commit()
    return app.test_client(), founder, holder


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def badge_lines(client, user):
    """The role line of `user`'s profile and of their preview, as the founder sees them."""
    lines = []
    for path, label in ((f'/u/{user.user_name}', 'Role permissions:'), (f'/user/{user.id}/preview', 'Roles:')):
        page = client.get(path).get_data(as_text=True)
        lines.append(next((line.strip() for line in page.splitlines() if label in line), None))
    return lines


def role(name, listed):
    made = Role(name=name, listed_publicly=listed)
    db.session.add(made)
    db.session.commit()
    return made


def test_the_seeded_staff_role_is_shown_by_its_name(people):
    client, founder, holder = people
    grant_role(holder, staff_role())
    login(client, founder)
    assert [line and 'Staff' in line for line in badge_lines(client, holder)] == [True, True]


def test_a_role_listed_publicly_is_shown_by_its_own_name_and_one_unlisted_is_not(people):
    client, founder, holder = people
    grant_role(holder, role('Helpers', listed=True))
    grant_role(holder, role('Quiet ones', listed=False))
    login(client, founder)
    lines = badge_lines(client, holder)
    assert [('Helpers' in line, 'Quiet ones' in line, 'Staff' in line) for line in lines] == [(True, False, False)] * 2


def test_staff_renamed_and_unlisted_shows_nothing(people):
    """Staff is an ordinary role: what an admin makes of it is what the profile shows."""
    client, founder, holder = people
    staff = staff_role()
    staff.name, staff.listed_publicly = 'Backroom', False
    db.session.commit()
    grant_role(holder, staff)
    login(client, founder)
    assert badge_lines(client, holder) == [None, None]


def test_an_expired_grant_is_not_shown(people):
    client, founder, holder = people
    helpers = role('Helpers', listed=True)
    db.session.execute(role_grant.insert().values(user_id=holder.id, role_id=helpers.id,
                                                  expires_at=utcnow() - timedelta(days=1)))
    db.session.commit()
    login(client, founder)
    assert badge_lines(client, holder) == [None, None]


def test_listed_role_names_are_in_name_order(people):
    _, _, holder = people
    for name in ('Zebra keepers', 'Archivists'):
        grant_role(holder, role(name, listed=True))
    assert holder.listed_role_names() == ['Archivists', 'Zebra keepers']


# --------------------------------------------------------------------------
# /about and the API's site admins name the listed roles' holders, not Staff by name
# --------------------------------------------------------------------------

def about_names(client):
    page = client.get('/about').get_data(as_text=True)
    line = next((line for line in page.splitlines() if 'It is moderated by' in line), '')
    return line


def test_about_lists_the_holders_of_each_listed_role(people):
    client, founder, holder = people
    other = make_user(holder.instance, 'unlisted', local=True)
    grant_role(holder, role('Helpers', listed=True))
    grant_role(other, role('Quiet ones', listed=False))
    line = about_names(client)
    assert 'holder' in line and 'unlisted' not in line


def test_about_drops_staff_once_an_admin_unlists_it(people):
    client, founder, holder = people
    staff = staff_role()
    grant_role(holder, staff)
    assert 'holder' in about_names(client)
    staff.listed_publicly = False
    db.session.commit()
    assert 'holder' not in about_names(client)


def test_listed_role_holders_are_each_listed_once_and_skip_the_banned_and_the_scoped(people):
    from app.models import Site, SiteDomain
    _, _, holder = people
    banned = make_user(holder.instance, 'banned', local=True)
    banned.banned = True
    scoped = make_user(holder.instance, 'scoped', local=True)
    domain = SiteDomain(domain='x.listed.example', scheme='https', federates=False)
    db.session.add(domain)
    db.session.commit()
    first, second = role('Helpers', listed=True), role('Greeters', listed=True)
    for user in (holder, banned):
        grant_role(user, first)
    grant_role(holder, second)
    grant_role(scoped, first, site_domain_id=domain.id)
    assert [user.id for user in Site.listed_role_holders()] == [holder.id]


def test_the_settings_that_named_staff_name_what_they_mean(app, people):
    """Spec, Settings that named Staff: the stored values stay 'staff'; their labels name the permission."""
    from app.admin.forms import SiteDomainForm, SiteMiscForm
    with app.test_request_context():
        referral = {choice.value: choice.label for choice in SiteMiscForm().referral_generators.choices}
        registration = {choice.value: choice.label for choice in SiteDomainForm().registration.choices}
    assert str(referral['staff']) == 'Admins and holders of "generate referral codes"'
    assert str(registration['staff']) == 'User administrators only'
