"""`flask init-db` makes the default SiteDomain row, as migration f1c3a9d7b2e4 does for an upgraded site.

init-db drops every table and builds the schema from the models, so the row the migration seeded is gone, and
default_domain() synthesises one with id 1 that no table holds. The first local row stamped with it -- the
microblogs community, the admin account -- then failed its site_domain_id foreign key: a new install made with
init-db could not create a community (found bringing the interop harness up fresh, 2026-10-28).
"""
from unittest.mock import MagicMock, patch

import pytest

from app import cli
from app.models import Instance, SiteDomain


@pytest.fixture
def commands(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app.test_cli_runner()


def _added_by_init_db(commands, app, server_name):
    """What init-db adds, with everything that would touch the real schema stood in, as test_cli_fetch_caps does."""
    fake_db = MagicMock()
    fake_db.inspect.return_value.get_table_names.return_value = ['alembic_version']
    fake_db.session.execute.return_value.fetchone.return_value = ('UTF8',)
    with patch.dict(app.config, {'SERVER_NAME': server_name}), patch('app.cli.db', fake_db), \
            patch('app.cli.retrieve_block_list', return_value=''), \
            patch('app.cli.retrieve_peertube_block_list', return_value=''):
        result = commands.invoke(args=['init-db', '--interactive', 'no'])
    assert result.exception is None, result.exception
    return [call.args[0] for call in fake_db.session.add.call_args_list]


def test_init_db_makes_the_default_domain_after_the_local_instance(commands, app):
    added = _added_by_init_db(commands, app, 'piefed.test')

    domains = [row for row in added if isinstance(row, SiteDomain)]
    assert [(row.domain, row.is_default, row.federates) for row in domains] == [('piefed.test', True, True)]
    instance_at = next(i for i, row in enumerate(added) if isinstance(row, Instance))
    assert added.index(domains[0]) > instance_at   # the before_insert hook gives the default instance row 1


def test_the_default_domain_is_the_bare_host_of_server_name(commands, app):
    added = _added_by_init_db(commands, app, 'Piefed.Test:8030')

    assert [row.domain for row in added if isinstance(row, SiteDomain)] == ['piefed.test']
