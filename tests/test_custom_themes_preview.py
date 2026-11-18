"""Theme holders preview a custom theme (custom themes plan, Task 8; spec §3.5).

A preview is a session flag: the domain's pages show the theme to that holder alone, over their own theme and the
domain's default, while they still manage the domain's themes and the theme still counts there. A session is per
host, so a preview is started from the domain it is for."""
import pytest

from app import db
from app.custom_themes import THEME_PREVIEW, custom_identifier, theme_version
from app.models import CustomTheme, role_grant
from tests.conftest import forget_login
from tests.test_custom_themes_admin import BASE, Actor, themes_url, world  # noqa: F401  (world is a fixture)
from tests.two_domains import two_domains  # noqa: F401  (a fixture world needs)

pytestmark = pytest.mark.usefixtures('site')
BANNER = 'You are previewing the theme'


def stylesheet(theme):
    return f'/theme/{theme.id}/styles.css?v={theme_version(theme)}'


def start(world, theme):
    return world['admin'].post(themes_url(world['default'], f'/{theme.id}/preview'))


def home(actor):
    response = actor.get('/')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def preview_flag(actor):
    with actor.client.session_transaction(base_url=BASE) as session:
        return session.get(THEME_PREVIEW)


def test_a_preview_shows_the_theme_to_its_holder_alone(app, world):
    day = world['day']
    response = start(world, day)
    assert response.status_code == 302 and response.headers['Location'] == '/'
    page = home(world['admin'])
    assert stylesheet(day) in page
    assert f'{BANNER} Daylight' in page
    forget_login()
    anonymous = app.test_client().get('/', base_url=BASE).get_data(as_text=True)
    assert stylesheet(day) not in anonymous and BANNER not in anonymous
    assert f'/theme/{day.id}/' not in home(world['holder'])


def test_a_preview_wins_over_the_holders_own_theme(world):
    admin_user = world['admin'].user
    admin_user.theme = 'amethyst'
    db.session.commit()
    start(world, world['day'])
    page = home(world['admin'])
    assert stylesheet(world['day']) in page and 'themes/amethyst/styles.css' not in page


def test_a_retired_theme_can_be_previewed(world):
    day = world['day']
    day.retired = True
    db.session.commit()
    start(world, day)
    assert stylesheet(day) in home(world['admin'])


def test_stopping(world):
    start(world, world['day'])
    world['admin'].post('/admin/theme-preview/stop')
    assert preview_flag(world['admin']) is None
    page = home(world['admin'])
    assert BANNER not in page and f'/theme/{world["day"].id}/' not in page


def test_a_preview_is_started_from_its_own_domain(world):
    night = world['night']
    response = world['admin'].post(themes_url(world['onion'], f'/{night.id}/preview'))
    assert response.status_code == 400
    assert preview_flag(world['admin']) is None
    page = world['admin'].get(themes_url(world['onion'])).get_data(as_text=True)
    assert 'To preview a theme, open this page on' in page
    assert f'/{night.id}/preview' not in page
    assert f'/{world["day"].id}/preview' in world['admin'].get(themes_url(world['default'])).get_data(as_text=True)


def test_another_domains_preview_is_ignored_here(world):
    with world['admin'].client.session_transaction(base_url=BASE) as session:
        session[THEME_PREVIEW] = {'domain_id': world['onion'].id, 'theme': custom_identifier(world['night'].id)}
    page = home(world['admin'])
    assert BANNER not in page and f'/theme/{world["night"].id}/' not in page
    assert preview_flag(world['admin']) is not None      # kept: it is for the onion


def test_losing_the_permission_ends_the_preview(world):
    start(world, world['day'])
    db.session.execute(role_grant.delete().where(role_grant.c.user_id == world['admin'].user.id))
    db.session.commit()
    page = home(world['admin'])
    assert BANNER not in page and f'/theme/{world["day"].id}/' not in page
    assert preview_flag(world['admin']) is None


def test_deleting_the_theme_ends_the_preview(world):
    day = world['day']
    start(world, day)
    db.session.delete(db.session.get(CustomTheme, day.id))
    db.session.commit()
    assert BANNER not in home(world['admin'])
    assert preview_flag(world['admin']) is None


@pytest.mark.parametrize('flag', ['not a dict', {'theme': 'custom:1'}, {'domain_id': None}])
def test_a_malformed_flag_is_ignored(world, flag):
    with world['admin'].client.session_transaction(base_url=BASE) as session:
        session[THEME_PREVIEW] = flag
    assert BANNER not in home(world['admin'])


def test_the_banner_shows_in_a_dillo_based_theme(world):
    day = world['day']
    day.base_theme = 'dillo'
    db.session.commit()
    start(world, day)
    page = home(world['admin'])
    assert '<body class="">' in page and f'{BANNER} Daylight' in page


def test_another_domains_theme_cannot_be_previewed(world):
    response = world['admin'].post(themes_url(world['default'], f'/{world["night"].id}/preview'))
    assert response.status_code == 404
    assert world['holder'].post(themes_url(world['default'], f'/{world["day"].id}/preview')).status_code == 403
