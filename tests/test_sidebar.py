"""Admins order the sidebars' boxes and hide some of them, the site and each domain its own (app/sidebar.py).

Owner rulings, 2026-11-12: both sidebars, the home page's and the community pages', each its own order (S1); per
domain, a domain without its own order showing the site's (S2); a box can be hidden as well as moved, and a shown box
with nothing in it still shows nothing (S3); one POST per up, down, hide or show button, no JavaScript (S4)."""
import pytest
from sqlalchemy import inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import db
from app.branding import branding_for
from app.models import DomainConfig, Site
from app.sidebar import BOXES, COMMUNITY, HOME, OPTIONAL, layout, moved, shown_boxes, sidebar_boxes, stored, toggled
from app.storefront import clear_domain_cache
from tests.schema_history import DomainConfigMigrations
from tests.conftest import forget_login
from tests.factories import make_user
from tests.test_admin_domain_branding import REPO
from tests.test_admin_site_profile import csrf, login
from tests.test_branding import load_migration
from tests.test_branding_pages import onion_branding, page
from tests.two_domains import default_request, onion_request
from tests.factories import config_of
from app.domain_config import own_config

pytestmark = pytest.mark.usefixtures('site')

HOME_DEFAULT = list(BOXES[HOME])
COMMUNITY_DEFAULT = list(BOXES[COMMUNITY])


def default(area):
    """Every box of `area` in the default order, an optional one hidden."""
    return [(name, name not in OPTIONAL) for name in BOXES[area]]


# ---------------------------------------------------------------------------------------------------------------------
# Reading and changing a stored order

def test_a_blank_order_is_every_box_in_the_default_order_the_optional_ones_hidden():
    assert layout('', HOME) == default(HOME)
    assert layout(None, COMMUNITY) == default(COMMUNITY)


def test_a_stored_order_is_read_with_its_hidden_boxes():
    boxes = layout('about,-new_instances,active_communities', HOME)
    assert boxes[:3] == [('about', True), ('new_instances', False), ('active_communities', True)]


@pytest.mark.parametrize('value', ['about,nonsense,about,-about', ' about , ,--about', 'about,'],
                         ids=['unknown-and-repeats', 'spaces-and-blanks', 'trailing-comma'])
def test_unknown_names_and_repeats_are_dropped_and_missing_boxes_shown_after(value):
    boxes = layout(value, HOME)
    assert [name for name, _shown in boxes] == ['about'] + [name for name in HOME_DEFAULT if name != 'about']
    assert all(shown == (name not in OPTIONAL) for name, shown in boxes)


def test_a_stored_order_reads_back_as_itself():
    boxes = layout('-recent_tags,about', HOME)
    assert layout(stored(boxes), HOME) == boxes
    assert stored(boxes).startswith('-recent_tags,about,')


@pytest.mark.parametrize('name, step, first_three', [
    ('active_communities', -1, ['active_communities', 'new_communities', 'upcoming_events']),
    ('new_communities', 1, ['active_communities', 'new_communities', 'upcoming_events']),
    ('new_communities', -1, ['new_communities', 'active_communities', 'upcoming_events']),
], ids=['up', 'down', 'up-at-the-top'])
def test_moving_a_box(name, step, first_three):
    assert [box for box, _shown in moved(layout('', HOME), name, step)][:3] == first_three


def test_moving_the_last_box_down_leaves_it_last():
    boxes = moved(layout('', HOME), HOME_DEFAULT[-1], 1)
    assert boxes == layout('', HOME)


def test_toggling_hides_then_shows_one_box():
    hidden = toggled(layout('', HOME), 'about')
    assert dict(hidden)['about'] is False and sum(not shown for _name, shown in hidden) == 1 + len(OPTIONAL)
    assert toggled(hidden, 'about') == layout('', HOME)


# ---------------------------------------------------------------------------------------------------------------------
# Which domain's order

def test_a_domain_without_its_own_order_shows_the_sites(app, two_domains, site):
    first, onion = two_domains.domains
    site.home_sidebar = 'about,-new_communities'
    db.session.commit()
    clear_domain_cache()
    assert branding_for(onion).home_sidebar == 'about,-new_communities'
    with onion_request(app):
        assert shown_boxes(HOME)[0] == 'about' and 'new_communities' not in shown_boxes(HOME)


def test_a_domains_own_order_is_its_alone(app, two_domains, site):
    first, onion = two_domains.domains
    site.community_sidebar = '-actions'
    config_of(onion).community_sidebar = 'related_communities'
    db.session.commit()
    clear_domain_cache()
    with onion_request(app):
        assert shown_boxes(COMMUNITY) == ['related_communities'] + \
            [name for name in COMMUNITY_DEFAULT if name not in OPTIONAL | {'related_communities'}]
    with default_request(app):
        assert 'actions' not in shown_boxes(COMMUNITY)


def test_sidebar_boxes_writes_the_shown_boxes_in_order_and_skips_empty_ones(app, two_domains, site):
    site.home_sidebar = 'recent_tags,about,-active_communities'
    db.session.commit()
    clear_domain_cache()
    with default_request(app):
        written = sidebar_boxes(HOME, {'about': '<p>about</p>', 'active_communities': '<p>active</p>',
                                       'recent_tags': '<p>tags</p>', 'new_communities': '  \n  '})
    assert str(written) == '<p>tags</p>\n<p>about</p>'


# ---------------------------------------------------------------------------------------------------------------------
# What pages show

def positions(body: str, markers: list[str]) -> list[int]:
    return [body.index(marker) for marker in markers]


def set_site(**values):
    site = db.session.get(Site, 1)
    for field, value in values.items():
        setattr(site, field, value)
    db.session.commit()
    clear_domain_cache()


def test_the_home_page_shows_its_boxes_in_the_default_order(app, branded):
    body = page(app, '/')
    first, second = positions(body, ['id="active_communities_card"', 'id="about_card"'])
    assert first < second


def test_the_home_page_shows_the_sites_order_and_hides_hidden_boxes(app, branded):
    set_site(home_sidebar='about,active_communities')
    body = page(app, '/')
    about, active = positions(body, ['id="about_card"', 'id="active_communities_card"'])
    assert about < active
    set_site(home_sidebar='-about')
    assert 'id="about_card"' not in page(app, '/')


def test_the_community_page_shows_the_sites_order(app, branded):
    body = page(app, '/c/goldart')
    actions, about = positions(body, ['id="community_actions"', 'id="about_community"'])
    assert actions < about
    set_site(community_sidebar='about_community,actions')
    body = page(app, '/c/goldart')
    actions, about = positions(body, ['id="community_actions"', 'id="about_community"'])
    assert about < actions
    set_site(community_sidebar='-actions')
    body = page(app, '/c/goldart')
    assert 'id="community_actions"' not in body and 'id="about_community"' in body


def test_the_onion_shows_its_own_order_and_the_first_domain_the_sites(app, branded):
    onion_branding(branded, home_sidebar='-about')
    assert 'id="about_card"' not in page(app, '/', onion=True)
    assert 'id="about_card"' in page(app, '/')


def test_the_dillo_theme_follows_the_order(app, branded):
    set_site(default_theme='dillo', home_sidebar='about,active_communities', community_sidebar='about_community')
    body = page(app, '/')
    about, active = positions(body, ['About Golden Site', 'Active communities'])
    assert about < active
    body = page(app, '/c/goldart')
    about, actions = positions(body, ['id="about_community"', 'id="community_actions"'])
    assert about < actions
    set_site(home_sidebar='-about', community_sidebar='-actions')
    assert 'About Golden Site' not in page(app, '/')
    assert 'id="community_actions"' not in page(app, '/c/goldart')


def test_each_box_after_the_first_has_a_gap_above():
    """The boxes no longer carry their own margins, which depended on their old fixed order: the stylesheet spaces
    whichever comes second and later."""
    for path in ('styles.css', 'styles.scss'):
        assert '> .card + .card' in (REPO / 'app' / 'static' / path).read_text()


# ---------------------------------------------------------------------------------------------------------------------
# The admin pages

def press(admin, path, action):
    client, token = admin
    forget_login()
    return client.post(path, data={'csrf_token': token, 'action': action})


def site_row() -> Site:
    db.session.expire_all()
    return db.session.get(Site, 1)


def own_row(domain) -> DomainConfig | None:
    db.session.expire_all()
    return own_config(domain.id)


def test_the_site_page_lists_both_sidebars_boxes(admin_domain_branding_admin, admin_domain_branding_domains):
    body = admin_domain_branding_admin[0].get('/admin/sidebar').get_data(as_text=True)
    for name in HOME_DEFAULT:
        assert f'id="box-home-{name}"' in body
    for name in COMMUNITY_DEFAULT:
        assert f'id="box-community-{name}"' in body
    assert 'reset:home' not in body   # the site has no other order to go back to


def test_the_buttons_move_hide_and_show_a_box_on_the_site(admin_domain_branding_admin, admin_domain_branding_domains):
    response = press(admin_domain_branding_admin, '/admin/sidebar', 'home:up:about')
    first = admin_domain_branding_domains['first']
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/sidebar?domain={first.id}#sidebar-home')
    assert layout(site_row().home_sidebar, HOME)[3:5] == [('about', True), ('recent_tags', True)]
    press(admin_domain_branding_admin, '/admin/sidebar', 'community:toggle:actions')
    assert dict(layout(site_row().community_sidebar, COMMUNITY))['actions'] is False
    press(admin_domain_branding_admin, '/admin/sidebar', 'community:toggle:actions')
    assert dict(layout(site_row().community_sidebar, COMMUNITY))['actions'] is True
    press(admin_domain_branding_admin, '/admin/sidebar', 'community:down:actions')
    assert layout(site_row().community_sidebar, COMMUNITY)[1] == ('actions', True)
    assert layout(site_row().home_sidebar, HOME)[3][0] == 'about'   # the other sidebar is untouched


def test_the_page_shows_the_result(admin_domain_branding_admin, admin_domain_branding_domains):
    press(admin_domain_branding_admin, '/admin/sidebar', 'home:toggle:new_instances')
    body = admin_domain_branding_admin[0].get('/admin/sidebar').get_data(as_text=True)
    row = body[body.index('id="box-home-new_instances"'):]
    assert row.index('Hidden') < row.index('</tr>')
    assert row.index('value="home:toggle:new_instances"') < row.index('</tr>')


@pytest.mark.parametrize('action', ['', 'home:up', 'home:sideways:about', 'home:up:nonsense', 'sidebar:up:about',
                                    'community:up:about', 'reset:nonsense', 'reset:home:about'],
                         ids=['none', 'no-box', 'unknown-button', 'unknown-box', 'unknown-sidebar', 'other-sidebars-box',
                              'reset-unknown', 'reset-too-long'])
def test_a_button_there_is_not_is_refused_and_changes_nothing(admin_domain_branding_admin, admin_domain_branding_domains, action):
    assert press(admin_domain_branding_admin, '/admin/sidebar', action).status_code == 400
    assert not site_row().home_sidebar and not site_row().community_sidebar


def test_a_post_without_csrf_is_refused(admin_domain_branding_admin, admin_domain_branding_domains):
    client, _token = admin_domain_branding_admin
    forget_login()
    assert client.post('/admin/sidebar', data={'action': 'home:up:about'}).status_code == 400
    assert not site_row().home_sidebar


def test_only_admins_see_or_change_the_sites_order(app, admin_domain_branding_domains):
    ordinary = make_user(admin_domain_branding_domains['instance'], 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)
    assert client.get('/admin/sidebar').status_code == 302    # permission_required sends them to permission denied
    forget_login()
    response = client.post('/admin/sidebar', data={'csrf_token': token, 'action': 'home:up:about'})
    assert response.status_code == 302 and 'permission' in response.headers['Location']
    assert not site_row().home_sidebar
    forget_login()
    response = client.get(f"/admin/sidebar?domain={admin_domain_branding_domains['shop'].id}")
    assert response.status_code == 302 and 'permission' in response.headers['Location']
    forget_login()
    response = client.post(f"/admin/sidebar?domain={admin_domain_branding_domains['shop'].id}",
                           data={'csrf_token': token, 'action': 'home:up:about'})
    assert response.status_code == 302 and 'permission' in response.headers['Location']
    assert own_row(admin_domain_branding_domains['shop']) is None


def test_a_domains_first_change_starts_from_the_sites_order(admin_domain_branding_admin, admin_domain_branding_domains):
    set_site(home_sidebar='about,-new_communities')
    shop = admin_domain_branding_domains['shop']
    body = admin_domain_branding_admin[0].get(f'/admin/sidebar?domain={shop.id}').get_data(as_text=True)
    assert "shows the site's order" in body and 'reset:home' not in body
    response = press(admin_domain_branding_admin, f'/admin/sidebar?domain={shop.id}', 'home:down:about')
    assert response.headers['Location'].endswith(f'/admin/sidebar?domain={shop.id}#sidebar-home')
    own = layout(own_row(shop).home_sidebar, HOME)
    assert own[:2] == [('new_communities', False), ('about', True)]
    assert own_row(shop).community_sidebar is None    # the other sidebar still shows the site's
    assert site_row().home_sidebar == 'about,-new_communities'
    assert branding_for(shop).home_sidebar == own_row(shop).home_sidebar


def test_a_domain_goes_back_to_the_sites_order(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    press(admin_domain_branding_admin, f'/admin/sidebar?domain={shop.id}', 'community:toggle:about_community')
    body = admin_domain_branding_admin[0].get(f'/admin/sidebar?domain={shop.id}').get_data(as_text=True)
    assert 'value="reset:community"' in body and 'value="reset:home"' not in body
    press(admin_domain_branding_admin, f'/admin/sidebar?domain={shop.id}', 'reset:community')
    assert own_row(shop).community_sidebar is None
    set_site(community_sidebar='-community_feeds')
    assert branding_for(shop).community_sidebar == '-community_feeds'


def test_a_domain_keeps_its_other_branding(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    config_of(shop).name = 'Shop name'
    db.session.commit()
    press(admin_domain_branding_admin, f'/admin/sidebar?domain={shop.id}', 'home:toggle:about')
    assert own_row(shop).name == 'Shop name' and own_row(shop).home_sidebar


def test_the_first_domains_page_is_the_sites(admin_domain_branding_admin, admin_domain_branding_domains):
    first = admin_domain_branding_domains['first']
    response = admin_domain_branding_admin[0].get(f"/admin/domains/{first.id}/sidebar")
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/sidebar?domain={first.id}')


def test_an_unknown_domain_is_404(admin_domain_branding_admin, admin_domain_branding_domains):
    assert admin_domain_branding_admin[0].get('/admin/domains/999/sidebar').status_code == 404


def test_the_onions_order_is_changed_like_any_domains(admin_domain_branding_admin, admin_domain_branding_domains):
    onion = admin_domain_branding_domains['onion']
    press(admin_domain_branding_admin, f'/admin/sidebar?domain={onion.id}', 'home:toggle:new_instances')
    assert dict(layout(own_row(onion).home_sidebar, HOME))['new_instances'] is False


def test_the_branding_pages_link_to_the_sidebar_pages(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    first = admin_domain_branding_domains['first']
    assert f'/admin/sidebar?domain={first.id}"' in admin_domain_branding_admin[0].get(f'/admin/site?domain={first.id}').get_data(as_text=True)
    assert f'/admin/sidebar?domain={shop.id}' in admin_domain_branding_admin[0].get(f'/admin/site?domain={shop.id}').get_data(as_text=True)
    assert f'/admin/sidebar?domain={shop.id}' in admin_domain_branding_admin[0].get('/admin/domains').get_data(as_text=True)


def test_a_change_shows_on_the_next_page(admin_domain_branding_admin, admin_domain_branding_domains):
    assert 'id="about_card"' in page_on(admin_domain_branding_admin, admin_domain_branding_domains)
    press(admin_domain_branding_admin, '/admin/sidebar', 'home:toggle:about')
    assert 'id="about_card"' not in page_on(admin_domain_branding_admin, admin_domain_branding_domains)


def page_on(admin, domains) -> str:
    forget_login()
    return admin[0].get('/').get_data(as_text=True)


# ---------------------------------------------------------------------------------------------------------------------
# The migration

def test_the_migration_adds_and_drops_the_sidebar_columns(db_session):
    migration = load_migration('*_sidebar_box_order.py')
    later = DomainConfigMigrations()   # it folded domain_branding and these columns into domain_config
    db.session.commit()

    def columns(table):
        return {column['name'] for column in inspect(db.session.connection()).get_columns(table)}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            later.downgrade()
            migration.downgrade()
            for table in ('site', 'domain_branding'):
                assert {'home_sidebar', 'community_sidebar'}.isdisjoint(columns(table))
            migration.upgrade()
            for table in ('site', 'domain_branding'):
                assert {'home_sidebar', 'community_sidebar'} <= columns(table)
            later.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()
