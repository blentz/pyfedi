"""app/reads/users.py: one person as a viewer may read them, and the query behind a list of people.

A person the viewer may not read is NoResultFound, for nobody and for a person hidden here alike (follow-up F6); the
alpha API's handler answers it `Not found`. The lookups by id and by name disagree on a deleted person (`BY_ID`,
`BY_NAME`); both spellings are pinned here.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import and_, func
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.models import User, utcnow
from app.reads.users import BY_ID, BY_NAME, People, person_by_id, person_listing, person_named, \
    refuse_hidden_person
from tests.factories import make_user
from tests.two_domains import ONION, default_request, onion_request, two_domains  # noqa: F401 -- a fixture


def remote_named(name, ap_domain):
    """The remote arm alpha's get_user hands person_named."""
    return and_(func.lower(User.user_name) == name, func.lower(User.ap_domain) == ap_domain)


@pytest.fixture
def world(app, api_baseline):
    remote = make_user(api_baseline.instance_remote, 'Faraway')
    return SimpleNamespace(local=api_baseline.user1, remote=remote, banned=api_baseline.banned_user,
                           remote_domain=api_baseline.instance_remote.domain, baseline=api_baseline)


@pytest.fixture
def onion_person(app, site, two_domains):  # noqa: F811 -- two_domains is the fixture
    """A local account of the local-only onion: hidden off the onion from everyone, and on it from a logged-out
    viewer."""
    return two_domains.local_user('hermit', two_domains.domains[1])


def listed(people: People, signed_in: bool = True) -> list[str]:
    """`people` as api_baseline's user3 (id 3, a signed-in reader who is no admin) is shown it, or nobody."""
    return [user.user_name for user in person_listing(3 if signed_in else None, people)]


class TestById:
    @pytest.mark.parametrize('signed_in', [False, True], ids=['anonymous', 'signed-in'])
    def test_a_person_is_found(self, world, signed_in):
        assert person_by_id(world.local.id, signed_in) is world.local

    def test_nobody_is_not_found(self, world):
        with pytest.raises(NoResultFound):
            person_by_id(987654, True)

    def test_a_deleted_person_is_found_by_id(self, world):
        world.local.deleted = True
        db.session.commit()
        assert person_by_id(world.local.id, True) is world.local

    def test_a_deleted_person_is_nobody_by_the_name_spelling(self, world):
        world.local.deleted = True
        db.session.commit()
        with pytest.raises(NoResultFound):
            person_by_id(world.local.id, True, BY_NAME)

    def test_a_person_hidden_here_is_not_found(self, app, onion_person):
        with default_request(app):
            with pytest.raises(NoResultFound):
                person_by_id(onion_person.id, True)


class TestByName:
    def test_a_bare_name_is_the_storefronts_account(self, world):
        assert person_named('user1', None) is world.local

    def test_a_remote_name_is_found_by_the_callers_condition(self, world):
        name, domain = 'faraway', world.remote_domain
        assert person_named(name, domain, remote_named(name, domain)) is world.remote

    def test_a_deleted_person_is_nobody(self, world):
        world.local.deleted = True
        db.session.commit()
        with pytest.raises(NoResultFound):
            person_named('user1', None)

    def test_a_deleted_person_is_found_by_the_id_spelling(self, world):
        world.local.deleted = True
        db.session.commit()
        assert person_named('user1', None, lookup=BY_ID) is world.local

    def test_nobody_is_not_found(self, world):
        with pytest.raises(NoResultFound):
            person_named('nobody', None)

    def test_a_hosted_domains_name_is_its_own_account(self, app, onion_person):
        with onion_request(app):
            assert person_named('hermit', ONION) is onion_person

    def test_a_hosted_domains_account_is_hidden_off_it(self, app, onion_person):
        with default_request(app):
            with pytest.raises(NoResultFound):
                person_named('hermit', ONION)


class TestHidden:
    @pytest.mark.parametrize('signed_in', [False, True], ids=['anonymous', 'signed-in'])
    def test_an_account_on_its_own_domain(self, app, onion_person, signed_in):
        with onion_request(app):
            if signed_in:
                assert refuse_hidden_person(onion_person, True) is None
            else:
                with pytest.raises(NoResultFound):
                    refuse_hidden_person(onion_person, False)

    def test_an_account_off_its_own_domain(self, app, onion_person):
        with default_request(app):
            with pytest.raises(NoResultFound):
                refuse_hidden_person(onion_person, True)

    def test_an_onion_account_is_left_out_of_the_list_off_the_onion(self, app, onion_person):
        with default_request(app):
            assert 'hermit' not in listed(People())
        with onion_request(app):
            assert 'hermit' in listed(People())
            assert 'hermit' not in listed(People(), signed_in=False)


class TestListing:
    def test_all_lists_every_account_not_deleted_banned_ones_too(self, world):
        world.baseline.user4.deleted = True
        db.session.commit()
        names = listed(People())
        assert {'user1', 'Faraway', 'banneduser'} <= set(names)
        assert 'user4' not in names

    def test_local_lists_this_storefronts_verified_unbanned_accounts(self, world):
        world.baseline.user3.verified = False
        db.session.commit()
        names = listed(People(scope='Local'))
        assert 'user1' in names
        assert {'Faraway', 'banneduser', 'user3'}.isdisjoint(names)

    @pytest.mark.parametrize('search, found', [('aRaW', 'Faraway'), ('@remote.piefed', 'Faraway'), ('user1', 'user1')],
                             ids=['by-name', 'by-address', 'local-name'])
    def test_a_search(self, world, search, found):
        assert found in listed(People(search=search))

    def test_an_address_search_finds_no_local_account(self, world):
        assert 'user1' not in listed(People(search='@'))

    @pytest.mark.parametrize('sort, first', [('New', 'newest'), ('Old', 'oldest'), ('TopWeek', 'prolific'),
                                             ('Hot', 'user1')], ids=['new', 'old', 'top', 'by-id'])
    def test_the_sorts(self, world, sort, first):
        for user in User.query.all():
            user.created, user.post_count = utcnow() - timedelta(days=100), 1
        for name, days, posts in (('newest', 0, 0), ('oldest', 900, 0), ('prolific', 30, 500)):
            made = make_user(world.baseline.instance_local, name, local=True)
            made.created, made.post_count = utcnow() - timedelta(days=days), posts
        db.session.commit()
        assert listed(People(sort=sort))[0] == first
