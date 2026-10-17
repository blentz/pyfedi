"""`main.add_post`: the "Create post" button outside any community.

It used to pick a community for the user -- the `cross_post_community_id` cookie's, else the first joined one -- and
redirect to its compose form. That form's post-type buttons are built from the community it opened on (Poll and Event
are left out for a Lemmy community, Audio shows only for a podcast), so the user posted with the wrong community's
choices. The route now shows the main menu's list of communities (favorites, moderating, joined) with its search box,
none preselected (owner ruling 2026-10-17), plus links to find or create another; `?community=<id>` sends the user on
to that community's form, keeping `&type=` when the compose form's own picker comes back through here.

A HARNESS NOTE. `client.set_cookie(key, value)` defaults the domain to `localhost`, while this app's `SERVER_NAME` is
`test.piefed.local`, so a cookie set without `domain=HOST` is never sent.
"""
import re

import pytest
from flask import g

from app import db
from app.constants import ROLE_ADMIN, ROLE_ADMIN_NAME
from app.models import Community, CommunityFavorite, CommunityMember, Language, Role, Site, User
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


def picks(html):
    """The community ids the picker links to, in page order."""
    return [int(found) for found in re.findall(r'href="/add_post\?community=(\d+)', html)]


def sections(html):
    return re.findall(r'data-community-section="(\w+)"', html)


class TestTheChooser:
    def test_the_button_shows_the_joined_communities_instead_of_opening_one(self, app, env):
        client, joined, elsewhere = env

        response = client.get('/add_post')

        assert response.status_code == 200
        assert picks(response.get_data(as_text=True)) == [joined.id]

    def test_the_communities_are_grouped_like_the_main_menu(self, app, env):
        client, joined, elsewhere = env
        alice = User.query.filter_by(user_name='alice').one()
        modded = make_community('modded')
        make_community_member(alice, modded, is_moderator=True)
        db.session.add(CommunityFavorite(user_id=alice.id, community_id=joined.id))
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert sections(html) == ['favorites', 'moderating', 'joined']
        assert picks(html) == [joined.id, modded.id, joined.id]

    def test_it_has_the_main_menus_search_box(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'id="community-picker-filter"' in html
        assert f'data-community-name="{joined.title.lower()}"' in html

    def test_no_community_is_preselected(self, app, env):
        """Owner ruling: the user picks every time, so a stale guess cannot send a post to the wrong place."""
        client, joined, elsewhere = env
        client.set_cookie('cross_post_community_id', str(joined.id), domain=HOST)

        html = client.get('/add_post').get_data(as_text=True)

        assert 'dropdown-item active' not in html

    def test_it_links_to_finding_a_community_that_is_not_listed(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/communities"' in html

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

    def test_a_user_who_has_joined_nothing_is_offered_finding_or_creating_one(self, app, env):
        """This was a bare 204, which a browser shows as the button doing nothing."""
        client, joined, elsewhere = env
        CommunityMember.query.delete()
        db.session.commit()

        response = client.get('/add_post')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert picks(html) == [] and 'id="community_picker"' not in html
        assert 'href="/communities"' in html and 'href="/community/add_local"' in html


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
        assert picks(response.get_data(as_text=True)) == [joined.id]

    def test_a_private_community_the_user_is_not_in_is_not_a_destination(self, app, env):
        """possible_communities() leaves it out of the chooser, so the redirect must not reach it by id either."""
        client, joined, elsewhere = env
        secret = make_community('secret')
        secret.private = True
        db.session.commit()

        response = client.get(f'/add_post?community={secret.id}')

        assert response.status_code == 200
        assert secret.id not in picks(response.get_data(as_text=True))


class TestWhoMayUseIt:
    def test_an_anonymous_visitor_is_sent_to_the_login(self, app, env):
        """`@login_required` -- the route reads `possible_communities()`, which needs a user."""
        response = app.test_client().get('/add_post')

        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert '/auth/login' in response.headers['Location']
