"""Interop D24: `flask refresh_discovery`, run daily from daily.sh and never on a request."""
from pathlib import Path

from app import cli
from app.discovery import refresh

ROOT = Path(__file__).resolve().parent.parent


def test_the_command_prints_each_sources_outcome(app, db_session, monkeypatch):
    monkeypatch.setattr('app.discovery.cli.refresh_discovery',
                        lambda: {'sepiasearch': 3, 'castopod-index': 'failed', 'expired': 1})
    cli.register(app)   # pyfedi.py registers the commands; the test app has none

    result = app.test_cli_runner().invoke(args=['refresh_discovery'])

    assert result.exception is None, result.exception
    assert 'sepiasearch: 3' in result.output
    assert 'castopod-index: failed' in result.output
    assert 'expired: 1' in result.output


def test_the_command_runs_the_real_refresh(app, db_session, monkeypatch):
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset())
    for source in list(refresh.FETCHERS):
        monkeypatch.setitem(refresh.FETCHERS, source, lambda exclude: [])
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['refresh_discovery'])

    assert result.exception is None, result.exception
    assert 'expired: 0' in result.output


def test_daily_sh_runs_the_refresh_after_daily_maintenance():
    lines = [line.strip() for line in (ROOT / 'daily.sh').read_text().splitlines()]

    assert lines.index('flask refresh_discovery') > lines.index('flask daily-maintenance')


def test_sync_discovery_reconciles_then_queues_polls(app, db_session, monkeypatch):
    from app.discovery import cli as discovery_cli
    order = []
    monkeypatch.setattr(discovery_cli, 'reconcile_sync', lambda: order.append('reconcile') or
                        {'added': 2, 'dropped': 1, 'refollowed': 0, 'failed_hosts': ['bad.example']})
    monkeypatch.setattr(discovery_cli, 'enqueue_polls', lambda: order.append('poll') or 3)
    cli.register(app)   # pyfedi.py registers the commands; the test app has none

    result = app.test_cli_runner().invoke(args=['sync_discovery'])

    assert result.exit_code == 0
    assert order == ['reconcile', 'poll']
    assert 'added: 2' in result.output and 'dropped: 1' in result.output
    assert 'failed hosts: bad.example' in result.output and 'polls queued: 3' in result.output


def test_daily_sh_syncs_after_refreshing_the_directory():
    lines = [line.strip() for line in (ROOT / 'daily.sh').read_text().splitlines()]

    assert lines.index('flask sync_discovery') > lines.index('flask refresh_discovery')
