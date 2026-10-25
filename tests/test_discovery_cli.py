"""Interop D24: `flask refresh_discovery`, run daily from daily.sh and never on a request."""
import subprocess
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


def test_sync_discovery_says_none_when_no_host_failed(app, db_session, monkeypatch):
    from app.discovery import cli as discovery_cli
    monkeypatch.setattr(discovery_cli, 'reconcile_sync',
                        lambda: {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []})
    monkeypatch.setattr(discovery_cli, 'enqueue_polls', lambda: 0)
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['sync_discovery'])

    assert 'failed hosts: none' in result.output


def _run_job_script(script, tmp_path, failing):
    """Run a job script with a stand-in `flask` that records each command and
    exits 1 for the one named `failing`. The script runs in an empty directory,
    so its `source venv/bin/activate` fails, as it does inside the container."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'ran'
    flask = bin_dir / 'flask'
    flask.write_text(f'#!/bin/bash\necho "$1" >> {log}\n[ "$1" != "{failing}" ]\n')
    flask.chmod(0o755)
    env = {'PATH': f'{bin_dir}:/usr/bin:/bin'}
    result = subprocess.run(['bash', str(ROOT / script)], cwd=tmp_path, env=env)
    return result.returncode, log.read_text().split()


def test_daily_sh_fails_when_a_step_fails_but_still_runs_the_rest(tmp_path):
    """On the first domain `flask daily-maintenance` crashed every night from
    2026-10-12, yet daily.sh exited 0 because its last command succeeded, so
    the systemd unit reported success and its OnFailure email never fired."""
    code, ran = _run_job_script('daily.sh', tmp_path, failing='daily-maintenance')

    assert code != 0
    assert ran == ['daily-maintenance', 'refresh_discovery', 'sync_discovery']


def test_daily_sh_succeeds_when_every_step_does(tmp_path):
    code, ran = _run_job_script('daily.sh', tmp_path, failing='none')

    assert code == 0
    assert ran == ['daily-maintenance', 'refresh_discovery', 'sync_discovery']


def test_email_notifs_sh_fails_when_a_step_fails_but_still_runs_the_rest(tmp_path):
    code, ran = _run_job_script('email_notifs.sh', tmp_path, failing='send_missed_notifs')

    assert code != 0
    assert ran == ['send_missed_notifs', 'process_email_bounces', 'clean_up_old_activities']


def test_email_notifs_sh_succeeds_when_every_step_does(tmp_path):
    code, ran = _run_job_script('email_notifs.sh', tmp_path, failing='none')

    assert code == 0
