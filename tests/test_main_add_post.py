"""`main.add_post`: the "Create post" button outside any community.

It used to pick a community for the user -- the `cross_post_community_id` cookie's, else the first joined one -- and
redirect to its compose form. That form's post-type buttons are built from the community it opened on (Poll and Event
are dead `#` links for a Lemmy community, Audio shows only for a podcast), and changing its community select did not
rebuild them, so the user posted with the wrong community's choices. The route now shows a chooser: the user picks a
community (none is preselected, owner ruling 2026-10-17) or creates one, and `?community=<id>` sends them on to that
community's form. The compose form's select comes back here with `&type=` when it changes, so the form is rebuilt for
the community picked there too.

A HARNESS NOTE. `client.set_cookie(key, value)` defaults the domain to `localhost`, while this app's `SERVER_NAME` is
`test.piefed.local`, so a cookie set without `domain=HOST` is never sent.
"""
import re

import pytest
from flask import g

from app import db
from app.constants import ROLE_ADMIN, ROLE_ADMIN_NAME
from app.models import Community, CommunityMember, Language, Role, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session):
    """Alice has joined `general`; `elsewhere` exists and she has not."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.community_creation_admin_only = False
    db.session.commit()
    local = make_instance(HOST)
    make_user(local, 'founder', local=True)          # id 1, the admin trap
    alice = make_user(local, 'alice', local=True)
    alice.verified = True
    joined = make_community('general')
    elsewhere = make_community('elsewhere')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(alice, joined)
    g.admin_ids = []
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(alice.id)
        sess['_fresh'] = True
    return client, joined, elsewhere


def options(html):
    """The chooser's <option value="id">s, in page order, and the ids marked selected."""
    found = re.findall(r'<option value="(\d*)"( selected)?', html)
    return [value for value, _ in found if value], [value for value, chosen in found if chosen]


class TestTheChooser:
    def test_the_button_shows_a_chooser_instead_of_opening_a_community(self, app, env):
        client, joined, elsewhere = env

        response = client.get('/add_post')

        assert response.status_code == 200
        values, _ = options(response.get_data(as_text=True))
        assert str(joined.id) in values and str(elsewhere.id) in values

    def test_the_communities_are_grouped_like_the_compose_forms_select(self, app, env):
        client, joined, elsewhere = env
        alice = User.query.filter_by(user_name='alice').one()
        make_community_member(alice, make_community('modded'), is_moderator=True)
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        groups = re.findall(r'<optgroup label="([^"]+)"', html)
        assert groups == ['Moderating', 'Joined communities', 'Others']

    def test_no_community_is_preselected(self, app, env):
        """Owner ruling: the user picks every time, so a stale guess cannot send a post to the wrong place."""
        client, joined, elsewhere = env

        _, selected = options(client.get('/add_post').get_data(as_text=True))

        assert selected == []

    def test_the_last_cross_posted_community_is_not_preselected_either(self, app, env):
        client, joined, elsewhere = env
        client.set_cookie('cross_post_community_id', str(elsewhere.id), domain=HOST)

        response = client.get('/add_post')

        assert response.status_code == 200
        assert options(response.get_data(as_text=True))[1] == []

    def test_it_offers_to_create_a_community(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' in html

    def test_it_does_not_offer_creation_when_only_admins_may_create(self, app, env):
        client, joined, elsewhere = env
        db.session.get(Site, 1).community_creation_admin_only = True
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' not in html

    def test_an_admin_is_offered_creation_when_only_admins_may_create(self, app, env):
        client, joined, elsewhere = env
        db.session.get(Site, 1).community_creation_admin_only = True
        db.session.commit()
        User.query.filter_by(user_name='alice').one().roles.append(Role(id=ROLE_ADMIN + 100, name=ROLE_ADMIN_NAME))
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' in html

    def test_a_user_with_nowhere_to_post_is_offered_creation_not_an_empty_reply(self, app, env):
        """This was a bare 204, which a browser shows as the button doing nothing."""
        client, joined, elsewhere = env
        CommunityMember.query.delete()
        Community.query.delete()
        db.session.commit()

        response = client.get('/add_post')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert options(html)[0] == []
        assert 'href="/community/add_local"' in html


class TestChoosingACommunity:
    def test_a_chosen_community_opens_its_compose_form(self, app, env):
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}')

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    @pytest.mark.parametrize('kind', ['link', 'discussion', 'image', 'video', 'audio', 'poll', 'event'])
    def test_the_type_the_compose_form_was_on_is_kept(self, app, env, kind):
        """The compose form's community select sends its current type, so switching community keeps it."""
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}&type={kind}')

        assert response.status_code == 302
        assert response.headers['Location'] == f'/community/elsewhere/submit/{kind}'

    def test_a_type_the_compose_form_does_not_have_is_dropped(self, app, env):
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}&type=../../admin')

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    @pytest.mark.parametrize('value', ['', 'abc', '1.5', '-1', '0', '999999', '٢', ' '])
    def test_a_value_naming_no_community_shows_the_chooser_again(self, app, env, value):
        """`'٢'` is an Arabic-Indic digit: str.isdigit() accepts it, so it pins that no int() parse can raise."""
        client, joined, elsewhere = env

        response = client.get('/add_post', query_string={'community': value})

        assert response.status_code == 200
        assert str(joined.id) in options(response.get_data(as_text=True))[0]

    def test_a_private_community_the_user_is_not_in_is_not_a_destination(self, app, env):
        """possible_communities() leaves it out of the chooser, so the redirect must not reach it by id either."""
        client, joined, elsewhere = env
        secret = make_community('secret')
        secret.private = True
        db.session.commit()

        response = client.get(f'/add_post?community={secret.id}')

        assert response.status_code == 200
        assert str(secret.id) not in options(response.get_data(as_text=True))[0]


class TestWhoMayUseIt:
    def test_an_anonymous_visitor_is_sent_to_the_login(self, app, env):
        """`@login_required` -- the route reads `possible_communities()`, which needs a user."""
        response = app.test_client().get('/add_post')

        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert '/auth/login' in response.headers['Location']
