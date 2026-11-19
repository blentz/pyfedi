"""Theme code: a preview shows a code theme's overrides to the previewing holder alone (theme code plan, Task 3)."""
import pytest

from app import db
from app.models import CustomThemeTemplate
from tests.conftest import forget_login
from tests.test_custom_themes_admin import BASE, world  # noqa: F401  (world is a fixture)
from tests.test_custom_themes_preview import home, start
from tests.two_domains import two_domains  # noqa: F401  (a fixture world needs)

pytestmark = pytest.mark.usefixtures('site')


def test_a_preview_renders_the_themes_overrides_for_the_previewer_alone(app, world):
    db.session.add(CustomThemeTemplate(custom_theme_id=world['day'].id, name='_home_nav.html', source='PREVIEWED NAV'))
    db.session.commit()
    start(world, world['day'])
    assert 'PREVIEWED NAV' in home(world['admin'])
    forget_login()
    assert 'PREVIEWED NAV' not in app.test_client().get('/', base_url=BASE).get_data(as_text=True)
