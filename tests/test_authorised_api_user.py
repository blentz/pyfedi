"""authorise_api_user takes an account another API already authorised (app.utils.Authorised: a Mastodon app's OAuth
token, plan W1.3) in place of the alpha API's JWT, and answers it as it answers a JWT's account."""
import pytest

from app import db
from app.utils import Authorised, authorise_api_user
from tests.factories import make_instance, make_user


@pytest.fixture
def person(app, api_baseline):
    return make_user(None, 'authorisedperson', local=True)


def test_it_answers_the_model_the_id_or_the_dict(person):
    assert authorise_api_user(Authorised(person), return_type='model') is person
    assert authorise_api_user(Authorised(person)) == person.id
    assert authorise_api_user(Authorised(person), return_type='dict')['id'] == person.id
    assert authorise_api_user(Authorised(person), id_match=person.id) == person.id


@pytest.mark.parametrize('what', ['banned', 'deleted', 'unverified', 'remote', 'nobody', 'someone-else'])
def test_an_account_that_may_not_act_is_refused(app, person, what):
    id_match = None
    if what == 'banned':
        person.banned = True
    elif what == 'deleted':
        person.deleted = True
    elif what == 'unverified':
        person.verified = False
    elif what == 'remote':
        person = make_user(make_instance('far.example'), 'farperson')
    elif what == 'nobody':
        person = None
    else:
        id_match = 2 ** 31 - 1
    db.session.commit()
    with pytest.raises(Exception, match='incorrect_login'):
        authorise_api_user(Authorised(person), id_match=id_match)


def test_a_lapsed_account_is_told_so(person, monkeypatch):
    monkeypatch.setattr(type(person), 'access_has_lapsed', lambda self: True)
    with pytest.raises(Exception, match='access_lapsed'):
        authorise_api_user(Authorised(person))
