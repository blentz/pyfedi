"""The docker compose stacks run background work in its own Celery worker.

On hell.cloud one worker took `celery,background,send` with four slots. A discovery sync queued hundreds of
backfills on `background`, they held the slots, and the instance's inbox (`process_inbox_request` on `celery`)
fell ~17,000 tasks behind. Every queue that `CELERY_ROUTES` names must still be consumed, and `background`
must never share a worker with `celery`."""
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / 'entrypoint_celery.sh'


def _routed_queues():
    source = (ROOT / 'app' / '__init__.py').read_text()
    return set(re.findall(r"\{'queue': '([a-z_]+)'\}", source)) | {'celery'}


def _default(variable):
    match = re.search(r'\$\{' + variable + r':-([^}]+)\}', ENTRYPOINT.read_text())
    assert match, f'{variable} has no default in entrypoint_celery.sh'
    return match.group(1)


def _workers(compose_file):
    services = yaml.safe_load((ROOT / compose_file).read_text())['services']
    workers = {}
    for name, service in services.items():
        if service.get('entrypoint') != './entrypoint_celery.sh':
            continue
        environment = service.get('environment') or {}
        queues = environment.get('CELERY_QUEUES', _default('CELERY_QUEUES'))
        workers[name] = set(queues.split(','))
    return workers


@pytest.mark.parametrize('compose_file', ['compose.yaml', 'compose.dev.yaml'])
def test_every_routed_queue_has_a_worker(compose_file):
    consumed = set().union(*_workers(compose_file).values())
    assert _routed_queues() <= consumed


@pytest.mark.parametrize('compose_file', ['compose.yaml', 'compose.dev.yaml'])
def test_background_work_never_shares_a_worker_with_the_inbox(compose_file):
    for name, queues in _workers(compose_file).items():
        assert not ('background' in queues and 'celery' in queues), name


def test_the_entrypoint_takes_its_queues_and_concurrency_from_the_environment():
    text = ENTRYPOINT.read_text()
    assert '--queues="${CELERY_QUEUES:-' in text and '--concurrency="${CELERY_CONCURRENCY:-' in text
