"""Custom themes per domain: storage and identifiers (custom themes plan, Task 1)."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app import db
from app.custom_themes import (CUSTOM_PREFIX, custom_identifier, custom_theme_id, custom_themes_for,
                               is_shipped, theme_counts)
from app.models import CustomTheme, CustomThemeFile, SiteDomain
from app.storefront import clear_domain_cache
from tests.two_domains import two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')


def make_theme(domain, name='Mine', base_theme='forest', css='', retired=False):
    theme = CustomTheme(site_domain_id=domain.id, name=name, base_theme=base_theme, css=css, retired=retired)
    db.session.add(theme)
    db.session.commit()
    return theme


@pytest.mark.parametrize('identifier, expected', [
    ('custom:1', 1), ('custom:42', 42), ('custom:999999999999', 999999999999),
    ('custom:0', None), ('custom:-1', None), ('custom:abc', None), ('custom:', None), ('custom:1a', None),
    ('custom:0001', None), ('custom:1000000000000', None), ('custom: 1', None), ('Custom:1', None),
    ('forest', None), ('piefed', None), ('', None), (None, None), (' custom:1', None), ('custom:１', None),
])
def test_custom_theme_id(identifier, expected):
    assert custom_theme_id(identifier) == expected


def test_custom_identifier_round_trips_and_fits_a_theme_column():
    assert custom_identifier(7) == f'{CUSTOM_PREFIX}7'
    assert custom_theme_id(custom_identifier(999999999999)) == 999999999999
    assert len(custom_identifier(999999999999)) <= 20


@pytest.mark.parametrize('name, expected', [
    ('forest', True), ('hacker_bbs_amber', True), ('hpt-fruits', True),
    ('piefed', False),                    # no directory: a theme, but not a shipped one
    ('hacker_bbs_shared', False),         # a directory with no <name>.json
    ('nope', False), ('', False), (None, False), ('custom:1', False),
    ('../themes/forest', False), ('forest/', False), ('.', False), ('..', False),
])
def test_is_shipped(name, expected):
    assert is_shipped(name) is expected


def test_theme_counts_shipped_and_piefed_everywhere(two_domains):
    default, onion = two_domains.domains
    for domain in (default, onion):
        assert theme_counts('piefed', domain)
        assert theme_counts('forest', domain)
        assert not theme_counts('nope', domain)
        assert not theme_counts('', domain)
        assert not theme_counts(None, domain)


def test_a_custom_theme_counts_only_on_its_own_domain(two_domains):
    default, onion = two_domains.domains
    theme = make_theme(onion)
    assert theme_counts(custom_identifier(theme.id), onion)
    assert not theme_counts(custom_identifier(theme.id), default)


def test_a_retired_custom_theme_still_counts(two_domains):
    _default, onion = two_domains.domains
    theme = make_theme(onion, retired=True)
    assert theme_counts(custom_identifier(theme.id), onion)


def test_a_deleted_custom_theme_counts_nowhere(two_domains):
    _default, onion = two_domains.domains
    theme = make_theme(onion)
    identifier = custom_identifier(theme.id)
    db.session.delete(theme)
    db.session.commit()
    assert not theme_counts(identifier, onion)


def test_deleting_a_domain_deletes_its_themes_and_their_files(two_domains):
    _default, onion = two_domains.domains
    theme = make_theme(onion)
    db.session.add(CustomThemeFile(custom_theme_id=theme.id, filename='a.png', path='/static/media/themes/x.png',
                                   mime_type='image/png', size=3))
    db.session.commit()
    theme_id = theme.id
    db.session.expunge_all()
    db.session.execute(db.delete(SiteDomain).where(SiteDomain.id == onion.id))
    db.session.commit()
    clear_domain_cache()
    assert db.session.get(CustomTheme, theme_id) is None
    assert db.session.query(CustomThemeFile).filter_by(custom_theme_id=theme_id).count() == 0


def test_names_are_unique_per_domain_ignoring_case(two_domains):
    default, onion = two_domains.domains
    make_theme(onion, name='Dusk')
    make_theme(default, name='Dusk')     # another domain may use the same name
    with pytest.raises(IntegrityError):
        make_theme(onion, name='dusk')
    db.session.rollback()


def test_file_names_are_unique_per_theme(two_domains):
    _default, onion = two_domains.domains
    first, second = make_theme(onion, name='One'), make_theme(onion, name='Two')
    for theme in (first, second):
        db.session.add(CustomThemeFile(custom_theme_id=theme.id, filename='a.png', path=f'/p/{theme.id}.png',
                                       mime_type='image/png', size=1))
    db.session.commit()
    db.session.add(CustomThemeFile(custom_theme_id=first.id, filename='a.png', path='/p/again.png',
                                   mime_type='image/png', size=1))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_custom_themes_for_orders_by_name_and_can_leave_out_retired(two_domains):
    default, onion = two_domains.domains
    make_theme(onion, name='beta')
    make_theme(onion, name='Alpha')
    make_theme(onion, name='Gamma', retired=True)
    make_theme(default, name='Other')
    assert [t.name for t in custom_themes_for(onion.id, include_retired=True)] == ['Alpha', 'beta', 'Gamma']
    assert [t.name for t in custom_themes_for(onion.id, include_retired=False)] == ['Alpha', 'beta']


def test_new_theme_defaults(two_domains):
    _default, onion = two_domains.domains
    theme = CustomTheme(site_domain_id=onion.id, name='Plain', base_theme='piefed')
    db.session.add(theme)
    db.session.commit()
    assert theme.css == ''
    assert theme.retired is False
    assert theme.created_at is not None and theme.updated_at is not None


def _migration(pattern):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/{pattern}'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_drops_and_makes_the_tables(db_session):
    """Downgrade then upgrade: the database is left as `flask db upgrade` made it."""
    migration, later = (_migration('*_custom_themes.py'), _migration('*_theme_code.py'))
    db.session.commit()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            later.downgrade()   # theme code's table refers to custom_theme: it goes first and comes back last
            migration.downgrade()
            assert not inspect(db.session.connection()).has_table('custom_theme')
            assert not inspect(db.session.connection()).has_table('custom_theme_file')
            migration.upgrade()
            assert inspect(db.session.connection()).has_table('custom_theme')
            assert {index['name'] for index in inspect(db.session.connection()).get_indexes('custom_theme')} >= \
                {'ix_custom_theme_domain_name', 'ix_custom_theme_site_domain_id'}
            later.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()
