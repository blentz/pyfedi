"""Every function app/activitypub/routes.py hands to Celery with `.delay(...)` must be a Celery task.

c131496c9 (D54) inserted process_announced_objects between `@celery.task` and `def process_delete_request`, so the
decorator moved to the new helper and every remote account deletion raised
`AttributeError: 'function' object has no attribute 'delay'` on /inbox outside debug. The inbox dispatch tests stub
these functions with recorders that have their own `.delay`, so they could not see it.
"""
import re
from pathlib import Path

import pytest

from app.activitypub import routes

SOURCE = (Path(routes.__file__)).read_text()
DELAYED = sorted(set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.delay\(', SOURCE)))


def test_routes_delays_something():
    assert 'process_delete_request' in DELAYED


@pytest.mark.parametrize('name', DELAYED)
def test_each_delayed_function_is_a_celery_task(name):
    target = getattr(routes, name)
    assert callable(getattr(target, 'delay', None)), f'{name} is called with .delay() but is not a Celery task'
