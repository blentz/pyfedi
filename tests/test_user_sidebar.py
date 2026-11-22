"""A signed-in user arranges the sidebars' boxes for themselves (app/sidebar.py, app/user/sidebar_routes.py; spec
2026-11-22-user-sidebar-order): the boxes their domain shows, in their own order, some hidden. A box the domain hides
stays hidden and is not offered; a user with no order of their own sees the domain's."""
from types import SimpleNamespace

import pytest
from flask_wtf.csrf import generate_csrf
from sqlalchemy import inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import db
from app.models import Site, User
from app.sidebar import BOXES, COMMUNITY, HOME, OPTIONAL, own_value, personal_layout, shown_boxes, stored
from app.storefront import clear_domain_cache
from app.user.sidebar_routes import apply_action
from tests.conftest import forget_login
from tests.factories import make_user
from tests.test_branding import load_migration
from tests.test_branding_pages import DEFAULT_BASE, log_in, onion_branding, page, visit
from tests.two_domains import default_request

HOME_DEFAULT = [name for name in BOXES[HOME] if name not in OPTIONAL]          # shown unless a value says otherwise
COMMUNITY_DEFAULT = [name for name in BOXES[COMMUNITY] if name not in OPTIONAL]


def shown(*names):
    return [(name, True) for name in names]
PATH = '/user/settings/sidebar'

pytestmark = pytest.mark.usefixtures('site')


# ---------------------------------------------------------------------------------------------------------------------
# Reading a user's order

def test_no_order_of_their_own_is_the_offered_boxes_as_offered():
    assert personal_layout(None, HOME, [('about', True), ('live_feed', False)]) == [('about', True), ('live_feed', False)]


def test_their_order_is_read_over_the_offered_boxes_only():
    boxes = personal_layout('recent_tags,-about,new_instances', HOME, shown('about', 'recent_tags', 'new_communities'))
    assert boxes == [('recent_tags', True), ('about', False), ('new_communities', True)]


def test_a_box_offered_later_is_shown_after_the_users_boxes():
    assert personal_layout('about,-recent_tags', HOME, shown('new_communities', 'about', 'recent_tags')) == \
        [('about', True), ('recent_tags', False), ('new_communities', True)]


@pytest.mark.parametrize('user, expected', [
    (SimpleNamespace(is_authenticated=False, home_sidebar='about'), None),
    (SimpleNamespace(is_authenticated=True, home_sidebar=None), None),
    (SimpleNamespace(is_authenticated=True, home_sidebar=''), None),
    (SimpleNamespace(is_authenticated=True, home_sidebar='-about'), '-about'),
    (None, None),
], ids=['anonymous', 'none', 'blank', 'own', 'no-user'])
def test_own_value(user, expected):
    assert own_value(user, HOME) == expected


# ---------------------------------------------------------------------------------------------------------------------
# Which boxes a request shows

@pytest.fixture
def reader(branded):
    user = make_user(None, 'reader', local=True)
    user.verified = True
    db.session.commit()
    return user


def set_site(**values):
    site = db.session.get(Site, 1)
    for field, value in values.items():
        setattr(site, field, value)
    db.session.commit()
    clear_domain_cache()


def set_reader(user, **values):
    for field, value in values.items():
        setattr(user, field, value)
    db.session.commit()


def shown_for(app, user, area):
    forget_login()
    with default_request(app):
        from flask_login import login_user
        if user is not None:
            login_user(user)
        try:
            return shown_boxes(area)
        finally:
            forget_login()


def test_an_anonymous_visitor_sees_the_domains_order(app, reader):
    set_site(home_sidebar='about,-new_communities')
    set_reader(reader, home_sidebar='new_communities,recent_tags')
    assert shown_for(app, None, HOME) == ['about'] + [name for name in HOME_DEFAULT
                                                      if name not in ('about', 'new_communities')]


def test_a_user_sees_their_own_order_and_hides_what_they_hid(app, reader):
    set_reader(reader, home_sidebar='recent_tags,-about', community_sidebar='-actions')
    assert shown_for(app, reader, HOME) == ['recent_tags'] + [name for name in HOME_DEFAULT
                                                              if name not in ('recent_tags', 'about')]
    assert shown_for(app, reader, COMMUNITY) == COMMUNITY_DEFAULT[1:]


def test_a_box_the_domain_hides_stays_hidden_whatever_the_user_chose(app, reader):
    set_site(home_sidebar='-about')
    set_reader(reader, home_sidebar='about,recent_tags')
    assert 'about' not in shown_for(app, reader, HOME)
    assert shown_for(app, reader, HOME)[0] == 'recent_tags'


def test_a_user_without_an_order_sees_the_domains(app, reader):
    set_site(community_sidebar='about_community,-actions')
    assert shown_for(app, reader, COMMUNITY)[0] == 'about_community'
    assert 'actions' not in shown_for(app, reader, COMMUNITY)


def positions(body, markers):
    return [body.index(marker) for marker in markers]


def test_the_home_and_community_pages_follow_the_users_order(app, reader):
    set_reader(reader, home_sidebar='about,active_communities', community_sidebar='about_community,-actions')
    about, active = positions(page(app, '/', user=reader), ['id="about_card"', 'id="active_communities_card"'])
    assert about < active
    body = page(app, '/c/goldart', user=reader)
    assert 'id="about_community"' in body and 'id="community_actions"' not in body
    active, about = positions(page(app, '/'), ['id="active_communities_card"', 'id="about_card"'])
    assert active < about   # everyone else still sees the site's order
    assert 'id="community_actions"' in page(app, '/c/goldart')


def test_the_order_follows_the_user_onto_another_domain_within_what_it_shows(app, reader, branded):
    onion_branding(branded, home_sidebar='-recent_tags')
    set_reader(reader, home_sidebar='-about')
    body = page(app, '/', onion=True, user=reader)
    assert 'id="about_card"' not in body


# ---------------------------------------------------------------------------------------------------------------------
# The buttons

OFFERED = {HOME: shown(*HOME_DEFAULT), COMMUNITY: shown('actions', 'about_community')}


def test_a_first_change_starts_from_the_offered_order():
    result = apply_action({HOME: None, COMMUNITY: None}, OFFERED, 'community:down:actions')
    assert result == {HOME: None, COMMUNITY: 'about_community,actions'}


def test_a_toggle_hides_then_shows():
    hidden = apply_action({HOME: None, COMMUNITY: None}, OFFERED, 'community:toggle:about_community')
    assert hidden[COMMUNITY] == 'actions,-about_community'
    assert apply_action(hidden, OFFERED, 'community:toggle:about_community')[COMMUNITY] == 'actions,about_community'


def test_a_move_skips_boxes_not_offered():
    """Only the offered boxes are stored, so moving one up passes the box next to it on the page."""
    result = apply_action({HOME: None, COMMUNITY: 'actions,upcoming_events,about_community'}, OFFERED,
                          'community:up:about_community')
    assert result[COMMUNITY] == 'about_community,actions'


def test_reset_goes_back_to_the_domains_order():
    assert apply_action({HOME: 'about', COMMUNITY: 'actions'}, OFFERED, 'reset:home') == \
        {HOME: None, COMMUNITY: 'actions'}


@pytest.mark.parametrize('action', ['', 'home:up', 'home:sideways:about', 'home:up:nonsense', 'sidebar:up:about',
                                    'community:up:about', 'community:up:related_communities', 'reset:nonsense',
                                    'reset:home:about'],
                         ids=['none', 'no-box', 'unknown-button', 'unknown-box', 'unknown-sidebar',
                              'other-sidebars-box', 'not-offered', 'reset-unknown', 'reset-too-long'])
def test_a_button_the_page_has_not_is_refused(action):
    assert apply_action({HOME: None, COMMUNITY: None}, OFFERED, action) is None


# ---------------------------------------------------------------------------------------------------------------------
# The settings page

@pytest.fixture
def signed_in(app, reader):
    forget_login()
    client = app.test_client()
    log_in(client, reader, DEFAULT_BASE)
    with app.test_request_context():
        from flask import session as flask_session
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction(base_url=DEFAULT_BASE) as session:
        session['csrf_token'] = raw
    return client, token


def press(signed_in, action, token=True):
    client, csrf_token = signed_in
    forget_login()
    data = {'action': action}
    if token:
        data['csrf_token'] = csrf_token
    return client.post(PATH, data=data, base_url=DEFAULT_BASE)


def reader_row() -> User:
    db.session.expire_all()
    return db.session.scalars(db.select(User).filter_by(user_name='reader')).one()


def settings_page(signed_in) -> str:
    forget_login()
    response = signed_in[0].get(PATH, base_url=DEFAULT_BASE)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_page_lists_the_boxes_the_domain_shows(app, signed_in):
    set_site(home_sidebar='-new_instances')
    body = settings_page(signed_in)
    for name in HOME_DEFAULT[:-1]:
        assert f'id="box-home-{name}"' in body
    assert HOME_DEFAULT[-1] == 'new_instances' and 'id="box-home-new_instances"' not in body
    for name in COMMUNITY_DEFAULT:
        assert f'id="box-community-{name}"' in body
    assert "You see the site's order" in body and 'reset:home' not in body


def test_the_buttons_change_the_users_order_alone(app, signed_in):
    response = press(signed_in, 'home:up:about')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'{PATH}#sidebar-home')
    assert reader_row().home_sidebar == stored(shown('new_communities', 'active_communities', 'upcoming_events',
                                                     'about', 'recent_tags', 'new_instances') + [('live_feed', False)])
    assert reader_row().community_sidebar is None
    assert not db.session.get(Site, 1).home_sidebar
    press(signed_in, 'community:toggle:actions')
    assert reader_row().community_sidebar.startswith('-actions,')
    body = settings_page(signed_in)
    row = body[body.index('id="box-community-actions"'):]
    assert row.index('Hidden') < row.index('</tr>')
    assert 'value="reset:community"' in body and 'You have your own order.' in body


def test_reset_clears_the_users_order(app, signed_in):
    press(signed_in, 'home:toggle:about')
    response = press(signed_in, 'reset:home')
    assert response.headers['Location'].endswith(f'{PATH}#sidebar-home')
    assert reader_row().home_sidebar is None


@pytest.mark.parametrize('action', ['', 'home:up:nonsense', 'reset:nowhere', 'home:toggle:new_instances'],
                         ids=['none', 'unknown-box', 'reset-unknown', 'hidden-by-the-domain'])
def test_a_button_there_is_not_is_refused_and_changes_nothing(app, signed_in, action):
    set_site(home_sidebar='-new_instances')
    assert press(signed_in, action).status_code == 400
    assert reader_row().home_sidebar is None and reader_row().community_sidebar is None


def test_a_post_without_csrf_is_refused(app, signed_in):
    assert press(signed_in, 'home:up:about', token=False).status_code == 400
    assert reader_row().home_sidebar is None


def test_a_visitor_who_is_not_signed_in_is_sent_to_log_in(app, reader):
    response = visit(app, PATH)
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_a_sidebar_the_site_hides_everything_in_still_offers_the_optional_boxes(app, signed_in):
    set_site(community_sidebar=','.join('-' + name for name in COMMUNITY_DEFAULT))
    body = settings_page(signed_in)
    community = body[body.index('id="sidebar-community"'):]
    assert 'id="box-community-live_feed"' in community   # optional: offered whatever the site chose
    for name in COMMUNITY_DEFAULT:
        assert f'id="box-community-{name}"' not in community


def test_the_settings_nav_links_to_it(app, signed_in):
    body = settings_page(signed_in)   # the nav every settings page includes, the button and the phone's dropdown
    assert f'href="{PATH}" class="btn btn-primary"' in body and f'value="{PATH}" selected' in body


def test_a_change_shows_on_the_next_page(app, signed_in, reader):
    assert 'id="about_card"' in page(app, '/', user=reader, client=signed_in[0])
    press(signed_in, 'home:toggle:about')
    assert 'id="about_card"' not in page(app, '/', user=reader, client=signed_in[0])


# ---------------------------------------------------------------------------------------------------------------------
# The migration

def test_the_migration_adds_and_drops_the_users_sidebar_columns(db_session):
    migration = load_migration('*_user_sidebar_order.py')
    db.session.commit()

    def columns():
        return {column['name'] for column in inspect(db.session.connection()).get_columns('user')}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            assert {'home_sidebar', 'community_sidebar'}.isdisjoint(columns())
            migration.upgrade()
            assert {'home_sidebar', 'community_sidebar'} <= columns()
        db.session.commit()
    finally:
        db.session.rollback()
