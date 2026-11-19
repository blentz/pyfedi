"""Theme code goes with a copy, whoever makes it (theme code plan, Task 8; spec §3.6, §3.7)."""
import pytest

from app import db
from app.models import CustomTheme, CustomThemeTemplate
from tests.test_custom_themes_admin import themes_url, world  # noqa: F401  (world is a fixture)
from tests.two_domains import two_domains  # noqa: F401  (a fixture world needs)

pytestmark = pytest.mark.usefixtures('site')


def test_a_holder_without_the_permission_copies_a_code_theme_with_its_code(world):
    night = world['night']
    night.script = 'go();'
    db.session.add(CustomThemeTemplate(custom_theme_id=night.id, name='index.html', source='home'))
    db.session.commit()
    response = world['holder'].post(themes_url(world['onion'], '/copy'), source=f'custom:{night.id}')
    assert response.status_code == 302
    copy = db.session.query(CustomTheme).filter_by(name='Copy of Night').one()
    assert copy.script == 'go();' and [(t.name, t.source) for t in copy.templates] == [('index.html', 'home')]
    assert [t.name for t in db.session.get(CustomTheme, night.id).templates] == ['index.html']   # the source keeps its


def test_a_shipped_copy_has_no_code(world):
    world['admin'].post(themes_url(world['default'], '/copy'), source='forest')
    copy = db.session.query(CustomTheme).filter(CustomTheme.name.like('Copy of %')).one()
    assert copy.script is None and copy.templates == []
