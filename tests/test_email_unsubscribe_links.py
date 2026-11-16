"""The unsubscribe links in member mail (newsletter, unread-notification digest).

A GET only asks. Mail scanners (Outlook's Safe Links, Gmail's prefetch, corporate gateways) follow every link in a
message, and a GET that unsubscribed opted members out without their knowledge. A POST unsubscribes: the page's
button, or a mail client's RFC 8058 one-click POST to the List-Unsubscribe URL.
"""
import pytest

from app import db
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

LINKS = [('newsletter', 'newsletter'), ('email_notifs', 'email_unread')]


@pytest.fixture
def member(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'reader', local=True)
    user.verification_token = 'tok123'
    user.newsletter = True
    user.email_unread = True
    db.session.commit()
    return user


def url(kind, user, token='tok123'):
    return f'/user/{kind}/{user.id}/{token}/unsubscribe'


@pytest.mark.parametrize('kind, column', LINKS)
def test_following_the_link_only_asks(app, member, kind, column):
    response = app.test_client().get(url(kind, member))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'method="post"' in page
    assert url(kind, member) in page
    db.session.refresh(member)
    assert getattr(member, column) is True


@pytest.mark.parametrize('kind, column', LINKS)
def test_the_button_unsubscribes(app, member, kind, column):
    response = app.test_client().post(url(kind, member))
    assert response.status_code == 200
    assert 'Unsubscribed' in response.get_data(as_text=True)
    db.session.refresh(member)
    assert getattr(member, column) is False


@pytest.mark.parametrize('kind, column', LINKS)
def test_a_mail_client_s_one_click_post_unsubscribes(app, member, kind, column):
    response = app.test_client().post(url(kind, member), data={'List-Unsubscribe': 'One-Click'})
    assert response.status_code == 200
    db.session.refresh(member)
    assert getattr(member, column) is False


@pytest.mark.parametrize('kind, column', LINKS)
def test_a_wrong_token_changes_nothing_and_says_nothing(app, member, kind, column):
    response = app.test_client().post(url(kind, member, token='wrong'))
    assert response.status_code == 200
    db.session.refresh(member)
    assert getattr(member, column) is True


@pytest.mark.parametrize('kind', [kind for kind, _ in LINKS])
def test_each_one_leaves_the_other_alone(app, member, kind):
    app.test_client().post(url(kind, member))
    db.session.refresh(member)
    assert (member.newsletter, member.email_unread) == ((False, True) if kind == 'newsletter' else (True, False))
