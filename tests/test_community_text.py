"""A community's wiki pages, title, description and rules are for its own moderators (spec 2026-11-03-rbac-design.md,
follow-up 2, owner ruling 2026-11-05). Instance roles, the built-in Admin included, lose them and keep the
administrative settings and removal. For an abandoned community, an admin appoints a moderator first.
"""
import pytest
from flask import g, session
from flask_wtf.csrf import generate_csrf

from app import db
from app.api.alpha.utils.community import put_community
from app.models import Community, CommunityWikiPage, Language
from tests.factories import (bearer, grant_permission, make_admin, make_community_member, seed_post_context)


@pytest.fixture
def s(db_session):
    s = seed_post_context(community_name='textroom')
    s.community.title = 'The moderators wrote this'
    s.community.description = 'Their description'
    s.community.rules = 'Their rules'
    s.community.private = False
    s.wiki = CommunityWikiPage(community_id=s.community.id, title='A page', slug='a-page', body='body',
                               body_html='<p>body</p>', who_can_edit=0)
    db.session.add(s.wiki)
    db.session.add(Language(code='und', name='Undetermined'))   # every save of a community appends it
    s.author.verified = s.voter.verified = True
    db.session.commit()
    return s


def logged_in(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def stored():
    community = Community.query.filter_by(name='textroom').one()
    return community.title, community.description, community.rules


UNCHANGED = ('The moderators wrote this', 'Their description', 'Their rules')


@pytest.fixture(params=['admin', 'holder'])
def instance_role(request, s):
    """An account with instance-wide reach over communities and no place in this one."""
    if request.param == 'admin':
        make_admin(s.voter)
    else:
        for permission in ('moderate all communities', 'administer all communities', 'administer communities pages'):
            grant_permission(s.voter, permission)
    return s.voter


# Wiki pages

def test_an_instance_role_may_not_edit_a_wiki_page(app, s, instance_role):
    assert s.wiki.can_edit(instance_role, s.community) is False


def test_a_moderator_may_edit_a_wiki_page(app, s):
    make_community_member(s.voter, s.community, is_moderator=True)

    assert s.wiki.can_edit(s.voter, s.community) is True


def test_an_instance_role_may_edit_a_page_open_to_every_member_as_any_member_may(app, s, instance_role):
    s.wiki.who_can_edit = 3
    db.session.commit()

    assert s.wiki.can_edit(instance_role, s.community) is True


def test_an_instance_role_may_not_add_a_wiki_page(app, s, instance_role):
    client = logged_in(app, instance_role)

    response = client.post('/community/textroom/moderate/wiki/add',
                           data={'title': 'New', 'slug': 'new', 'body': 'x', 'csrf_token': csrf(app, client)})

    assert response.status_code in (401, 403, 404)
    assert CommunityWikiPage.query.filter_by(slug='new').first() is None


def test_an_instance_role_may_still_delete_a_wiki_page(app, s, instance_role):
    """Removal stays."""
    client = logged_in(app, instance_role)
    page_id = s.wiki.id

    client.post(f'/community/textroom/moderate/wiki/{page_id}/delete', data={'csrf_token': csrf(app, client)})

    assert db.session.get(CommunityWikiPage, page_id) is None


def test_the_wiki_list_offers_an_instance_role_delete_and_not_edit(app, s, instance_role):
    page = logged_in(app, instance_role).get('/community/textroom/moderate/wiki').get_data(as_text=True)

    assert f'/moderate/wiki/{s.wiki.id}/delete' in page
    assert f'/moderate/wiki/{s.wiki.id}/edit' not in page


# The moderators' edit page

def edit_payload(app, client, s, **overrides):
    form = {'title': s.community.title, 'description': s.community.description, 'topic': '-1',
            'theme': 'disabled', 'invitations': str(s.community.invitations or 0),
            'languages': [], 'downvote_accept_mode': '0', 'csrf_token': csrf(app, client)}
    return {**form, **overrides}


def test_an_instance_role_cannot_change_the_text_on_the_edit_page_and_can_change_settings(app, s, instance_role):
    client = logged_in(app, instance_role)
    data = edit_payload(app, client, s, title='Rewritten', description='Rewritten', new_mods_wanted='y')

    response = client.post(f'/community/{s.community.id}/edit', data=data)

    assert response.status_code == 302
    db.session.expire_all()
    assert stored() == UNCHANGED
    assert db.session.get(Community, s.community.id).new_mods_wanted is True


def test_the_edit_page_shows_an_instance_role_the_text_as_read_only(app, s, instance_role, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the template renders the form's token
    page = logged_in(app, instance_role).get(f'/community/{s.community.id}/edit').get_data(as_text=True)

    title_input = page[page.index('id="title"') - 200:page.index('id="title"') + 200]
    assert 'disabled' in title_input


def test_a_community_with_no_description_is_no_change_of_text(app, s, instance_role):
    """A stored None is not told apart from what the API reads back: the holder is changing no text."""
    g.site = s.site
    g.admin_ids = []
    s.community.description = None
    s.community.rules = None
    db.session.commit()

    put_community(bearer(instance_role), {'community_id': s.community.id, 'nsfw': True})

    db.session.expire_all()
    assert db.session.get(Community, s.community.id).nsfw is True


def test_a_moderator_may_change_the_text_on_the_edit_page(app, s):
    make_community_member(s.voter, s.community, is_moderator=True)
    client = logged_in(app, s.voter)
    data = edit_payload(app, client, s, title='Rewritten', description='Rewritten')

    client.post(f'/community/{s.community.id}/edit', data=data)

    db.session.expire_all()
    assert stored()[:2] == ('Rewritten', 'Rewritten')


def test_an_admin_who_moderates_the_community_may_change_its_text(app, s):
    make_admin(s.voter)
    make_community_member(s.voter, s.community, is_moderator=True)
    client = logged_in(app, s.voter)

    client.post(f'/community/{s.community.id}/edit', data=edit_payload(app, client, s, title='Rewritten'))

    db.session.expire_all()
    assert stored()[0] == 'Rewritten'


# The admin area's edit page

def admin_payload(app, client, s, **overrides):
    form = {'title': s.community.title, 'url': s.community.name, 'description': s.community.description,
            'rules': s.community.rules, 'topic': '-1', 'default_layout': '', 'posting_warning': '',
            'downvote_accept_mode': '0', 'content_retention': '-1', 'csrf_token': csrf(app, client)}
    return {**form, **overrides}


def test_an_instance_role_cannot_change_the_text_in_the_admin_area_and_can_change_settings(app, s, instance_role):
    client = logged_in(app, instance_role)
    data = admin_payload(app, client, s, title='Rewritten', description='Rewritten', rules='Rewritten', nsfw='y')

    response = client.post(f'/admin/community/{s.community.id}/edit', data=data)

    assert response.status_code == 302
    db.session.expire_all()
    assert stored() == UNCHANGED
    assert db.session.get(Community, s.community.id).nsfw is True


def test_the_admin_page_shows_an_instance_role_the_text_as_read_only(app, s, instance_role, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the template renders the form's token
    page = logged_in(app, instance_role).get(f'/admin/community/{s.community.id}/edit').get_data(as_text=True)

    title_input = page[page.index('id="title"') - 200:page.index('id="title"') + 200]
    assert 'disabled' in title_input


# The API

@pytest.mark.parametrize('field', ['title', 'description', 'rules'])
def test_the_api_refuses_an_instance_role_changing_the_text(app, s, instance_role, field):
    g.site = s.site
    g.admin_ids = []

    with pytest.raises(Exception, match='community_text_is_for_moderators'):
        put_community(bearer(instance_role), {'community_id': s.community.id, field: 'Rewritten'})

    db.session.rollback()
    assert stored() == UNCHANGED


def test_the_api_lets_an_instance_role_change_settings_with_the_text_left_alone(app, s, instance_role):
    g.site = s.site
    g.admin_ids = []

    put_community(bearer(instance_role), {'community_id': s.community.id, 'title': s.community.title, 'nsfw': True})

    db.session.expire_all()
    assert stored() == UNCHANGED
    assert db.session.get(Community, s.community.id).nsfw is True


def test_the_api_lets_a_moderator_change_the_text(app, s):
    g.site = s.site
    g.admin_ids = []
    make_community_member(s.voter, s.community, is_moderator=True)

    put_community(bearer(s.voter), {'community_id': s.community.id, 'title': 'Rewritten'})

    db.session.expire_all()
    assert stored()[0] == 'Rewritten'
