"""Helpers for migration tests that run an older migration on the schema it was written for."""
import importlib.util
from pathlib import Path


def load_revision(pattern: str):
    """The migration module whose file matches `pattern` under migrations/versions."""
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/{pattern}'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DomainConfigMigrations:
    """Migrations b9e4d2a7c1f6 and c3f7a1e9d5b8 as one step (spec 2026-11-20-multitenant-domain-config): what they
    moved into domain_config goes back to site, site_domain, domain_branding and settings, and returns. An older
    migration's test undoes it first and redoes it last; call both inside an alembic Operations context."""

    def __init__(self):
        self.table = load_revision('b9e4d2a7c1f6_domain_config.py')
        self.settings = load_revision('c3f7a1e9d5b8_domain_config_settings.py')

    def downgrade(self):
        self.settings.downgrade()
        self.table.downgrade()

    def upgrade(self):
        self.table.upgrade()
        self.settings.upgrade()
