"""The web container's entrypoint (entrypoint.sh) starts the server whether or not data.lemmyverse.net answers:
`flask populate_community_search` only refreshes the list of communities the web suggests, and a slow lemmyverse
(more than 120 seconds, 2026-11-11) left the container exited and the instance down. A failed migration still stops
it. Run with stand-ins for flask and gunicorn on PATH."""
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAND_IN = '#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "$CALLS"\n{body}\n'


@pytest.fixture
def run(tmp_path):
    def run_entrypoint(flask_body: str):
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        for name, body in (('flask', flask_body), ('gunicorn', 'exit 0')):
            script = bin_dir / name
            script.write_text(STAND_IN.format(body=body))
            script.chmod(0o755)
        calls = tmp_path / 'calls'
        env = {'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'CALLS': str(calls), 'HOME': str(tmp_path)}
        result = subprocess.run(['bash', str(ROOT / 'entrypoint.sh')], env=env, cwd=tmp_path, capture_output=True,
                                text=True, timeout=30)
        return result, calls.read_text().splitlines() if calls.exists() else []
    return run_entrypoint


def test_the_server_starts_when_the_community_list_cannot_be_fetched(run):
    result, calls = run('[ "$1" = populate_community_search ] && exit 1; exit 0')
    assert result.returncode == 0, result.stderr
    assert calls == ['flask db upgrade', 'flask populate_community_search',
                     'gunicorn --config gunicorn.conf.py --preload pyfedi:app']
    assert 'community search list was not refreshed' in result.stdout


def test_a_failed_migration_still_stops_the_server(run):
    result, calls = run('[ "$1" = db ] && exit 1; exit 0')
    assert result.returncode != 0
    assert calls == ['flask db upgrade']
