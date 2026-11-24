"""The workers register the ATProto tasks (spec 2026-11-23-atproto-federation-design.md §4). No blueprint imports
app.atproto.ingest, so a worker that did not import it itself answered every Jetstream event with `Received
unregistered task of type 'app.atproto.ingest.event_task'` (the harness's first P3 run): each module whose tasks only
a consumer or a schedule sends is named in both worker entry points."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TASK_MODULES = {'app.atproto.actors', 'app.atproto.ingest', 'app.connections.bluesky_hosted'}


def imported(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.update(f'{node.module}.{alias.name}' for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize('entry', ['celery_worker_docker.py', 'celery_worker.default.py'])
def test_each_worker_imports_the_atproto_task_modules(entry):
    assert TASK_MODULES <= imported(ROOT / entry)


def test_their_tasks_are_registered_once_imported(app):
    from app import celery
    from app.atproto import actors, ingest   # noqa: F401
    from app.connections import bluesky_hosted   # noqa: F401
    for name in ('app.atproto.ingest.event_task', 'app.atproto.actors.backfill_task', 'app.atproto.actors.refresh_task',
                 'app.connections.bluesky_hosted.profile_task', 'app.connections.bluesky_hosted.daily_task'):
        assert name in celery.tasks
