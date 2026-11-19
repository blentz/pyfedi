"""Custom themes phase 6: the permission, storage and limits (theme code plan, Task 1; spec §3.1-3.2)."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import CustomTheme, CustomThemeTemplate, ModLog, Role, RolePermission
from app.permissions import CATALOGUE, SCOPABLE_PERMISSIONS, STAFF_PERMISSIONS, user_access
from tests.factories import grant_role, limit_role
from tests.test_custom_themes import make_theme
from tests.two_domains import two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')
CODE = 'manage theme code'


def holder_on(two_domains, domain, *permissions, name='holder'):
    """A local user holding `permissions` on `domain` alone, through a role limited to it."""
    two_domains.local_user('founder', two_domains.domains[0])   # user 1 holds everything: never an actor
    user = two_domains.local_user(name, two_domains.domains[0])
    role = Role(name=f'{name} on {domain.domain}')
    db.session.add(role)
    db.session.commit()
    db.session.add_all([RolePermission(role_id=role.id, permission=p) for p in permissions])
    db.session.commit()
    limit_role(role, domain)
    grant_role(user, role)
    return user


def test_the_permission_is_domain_scoped_not_staff_and_says_what_it_is_worth():
    permission = next(p for p in CATALOGUE if p.name == CODE)
    assert permission.group == 'domains' and CODE in SCOPABLE_PERMISSIONS and CODE not in STAFF_PERMISSIONS
    assert 'full instance admin' in permission.guards


def test_it_is_not_implied_by_branding(two_domains):
    onion = two_domains.domains[1]
    user = holder_on(two_domains, onion, 'edit domain branding')
    assert user_access('edit domain branding', user.id, on=onion)
    assert not user_access(CODE, user.id, on=onion)


def test_a_grant_on_one_domain_holds_there_alone(two_domains):
    default, onion = two_domains.domains
    user = holder_on(two_domains, onion, CODE)
    assert user_access(CODE, user.id, on=onion)
    assert not user_access(CODE, user.id, on=default)


def test_a_theme_has_a_script_and_templates_that_go_with_it(two_domains):
    theme = make_theme(two_domains.domains[0])
    assert theme.script is None and theme.templates == []
    theme.script = 'console.log(1);'
    db.session.add(CustomThemeTemplate(custom_theme_id=theme.id, name='index.html', source='{{ 1 }}'))
    db.session.commit()
    db.session.expire_all()
    theme = db.session.get(CustomTheme, theme.id)
    assert theme.script == 'console.log(1);' and [t.name for t in theme.templates] == ['index.html']
    db.session.delete(theme)
    db.session.commit()
    assert db.session.query(CustomThemeTemplate).count() == 0


def test_a_template_name_is_unique_within_a_theme(two_domains):
    theme = make_theme(two_domains.domains[0])
    db.session.add_all([CustomThemeTemplate(custom_theme_id=theme.id, name='index.html', source=''),
                        CustomThemeTemplate(custom_theme_id=theme.id, name='index.html', source='')])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_code_changes_are_logged_for_admins_only():
    actions = {'import_theme', 'edit_theme_template', 'remove_theme_template', 'edit_theme_script',
               'remove_theme_script'}
    assert actions <= ModLog.admin_only_actions and actions <= set(ModLog.action_map)


def test_limits(app):
    assert (app.config['CUSTOM_THEME_MAX_TEMPLATES'], app.config['CUSTOM_THEME_MAX_TEMPLATE_BYTES'],
            app.config['CUSTOM_THEME_MAX_SCRIPT_BYTES'], app.config['CUSTOM_THEME_MAX_ARCHIVE_BYTES']) == \
           (100, 256 * 1024, 512 * 1024, 12 * 1024 * 1024)


def test_migration_drops_and_makes_the_table_and_column(db_session):
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_theme_code.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.commit()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            found = inspect(db.session.connection())
            assert not found.has_table('custom_theme_template')
            assert 'script' not in {c['name'] for c in found.get_columns('custom_theme')}
            migration.upgrade()
            found = inspect(db.session.connection())
            assert found.has_table('custom_theme_template')
            assert 'script' in {c['name'] for c in found.get_columns('custom_theme')}
        db.session.commit()
    finally:
        db.session.rollback()
