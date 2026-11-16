"""An account's IP address, and the country it gave, are cleared a year after they were recorded (privacy policy,
2026-11-16): stamped whenever the address is stored, cleared daily, kept while the account is banned."""
import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import cli, db
from app.models import utcnow
from app.onion import ONION_ACCOUNT_IP, apply_audit
from app.shared.tasks.maintenance import IP_RETENTION_DAYS, clear_old_ip_addresses
from tests.factories import make_user


def with_ip(user, days_ago, ip='203.0.113.7', country='NZ', banned=False):
    user.ip_address, user.ip_address_country, user.banned = ip, country, banned
    user.ip_address_at = utcnow() - timedelta(days=days_ago)
    db.session.commit()
    return user


class TestStamping:
    def test_recording_an_address_dates_it(self, app, api_baseline):
        user = make_user(api_baseline.instance_local, 'member', local=True)
        before = utcnow()
        apply_audit(user, 'ip_address', '198.51.100.4')
        assert user.ip_address == '198.51.100.4' and user.ip_address_at >= before

    def test_something_without_a_date_is_set_alone(self, app, api_baseline):
        instance = api_baseline.instance_remote
        apply_audit(instance, 'ip_address', '198.51.100.9')
        assert instance.ip_address == '198.51.100.9' and not hasattr(instance, 'ip_address_at')


class TestClearing:
    def test_a_year_old_address_and_its_country_are_cleared(self, app, api_baseline):
        old = with_ip(make_user(api_baseline.instance_local, 'old', local=True), IP_RETENTION_DAYS + 1)
        assert clear_old_ip_addresses() == 1
        db.session.refresh(old)
        assert (old.ip_address, old.ip_address_country, old.ip_address_at) == (None, None, None)

    def test_one_recorded_within_the_year_is_kept(self, app, api_baseline):
        recent = with_ip(make_user(api_baseline.instance_local, 'recent', local=True), IP_RETENTION_DAYS - 1)
        assert clear_old_ip_addresses() == 0
        db.session.refresh(recent)
        assert recent.ip_address == '203.0.113.7'

    def test_a_banned_account_keeps_its_own_while_banned(self, app, api_baseline):
        banned = with_ip(make_user(api_baseline.instance_local, 'banned', local=True), IP_RETENTION_DAYS + 30, banned=True)
        assert clear_old_ip_addresses() == 0
        db.session.refresh(banned)
        assert banned.ip_address == '203.0.113.7'

    def test_an_onion_accounts_sentinel_is_no_address(self, app, api_baseline):
        onion = with_ip(make_user(api_baseline.instance_local, 'onion', local=True), IP_RETENTION_DAYS + 30,
                        ip=ONION_ACCOUNT_IP, country=None)
        assert clear_old_ip_addresses() == 0
        db.session.refresh(onion)
        assert onion.ip_address == ONION_ACCOUNT_IP

    def test_a_failure_rolls_back_and_is_raised(self, app, api_baseline, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', MagicMock(side_effect=RuntimeError('down')))
        with pytest.raises(RuntimeError):
            clear_old_ip_addresses()


@pytest.mark.parametrize('command', ['daily-maintenance', 'daily-maintenance-celery'])
def test_daily_maintenance_runs_it(app, db_session, monkeypatch, command):
    calls = []
    for name in ('cleanup_old_notifications', 'cleanup_old_read_posts', 'cleanup_send_queue', 'process_expired_bans',
                 'remove_old_community_content', 'remove_old_bot_content', 'expire_relayed_posts',
                 'update_hashtag_counts', 'update_community_stats', 'cleanup_old_voting_data', 'unban_expired_users',
                 'sync_defederation_subscriptions', 'recalculate_user_attitudes', 'calculate_community_activity_stats',
                 'cleanup_old_activitypub_logs', 'clean_up_tmp', 'delete_old_soft_deleted_content', 'archive_old_posts',
                 'archive_old_users', 'pwn_bots', 'log_cron_task_to_db', 'check_instance_health',
                 'monitor_healthy_instances', 'pseudonymise_settled', 'remove_unpaid_registrations',
                 'end_expired_subscriptions'):
        monkeypatch.setattr(cli, name, MagicMock())
    monkeypatch.setattr(cli, 'clear_old_ip_addresses', lambda: calls.append(1))
    monkeypatch.setattr(cli.plugins, 'fire_hook', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'get_setting', lambda *a, **k: False)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    cli.register(app)
    result = app.test_cli_runner().invoke(args=[command])
    assert result.exception is None, result.output
    assert calls == [1]


def load():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_ip_address_at.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_the_migration_dates_stored_addresses_by_when_the_account_was_last_seen(app, api_baseline):
    seen = utcnow() - timedelta(days=40)
    user = make_user(api_baseline.instance_local, 'stored', local=True)
    user.ip_address, user.last_seen = '203.0.113.7', seen
    bare = make_user(api_baseline.instance_local, 'bare', local=True)
    bare.ip_address = None
    db.session.commit()
    with Operations.context(MigrationContext.configure(db.session.connection())):
        load().downgrade()
        load().upgrade()
    rows = dict(db.session.execute(text('SELECT id, ip_address_at FROM "user" WHERE id IN (:a, :b)'),
                                   {'a': user.id, 'b': bare.id}).all())
    assert abs((rows[user.id] - seen).total_seconds()) < 1 and rows[bare.id] is None
