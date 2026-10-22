"""A Celery task acts as the domain that queued it (plan C3, ruling b).

`storefront.acting_domain()` names the domain an instance-level request is made as: in a web or API request, the
acting account's or the storefront's. A worker has no request, so without this every fetch a task made named the
default domain (hell.cloud), whoever caused it, and a task run inline (DEBUG, `send_async=False`) named another
domain than the same task queued. Here the domain is stamped into each task's message headers when it is queued and
entered for the task's run: `ActingTask` is every task's class (`Celery(task_cls=...)`), so every `delay` and
`apply_async`, eager or not, carries it; `task_prerun` enters `acting_for` it and `task_postrun`, sent even when the
task fails, leaves it, so nothing carries over to the next task on a worker. An explicit `acting_for` or
`@acting_as_default()` inside a task (the discovery credits, relays) still wins, being the inner block.
"""
from contextlib import ExitStack

from celery import Task
from celery.signals import task_postrun, task_prerun
from flask import has_app_context

from app.storefront import acting_domain, acting_for, domain_by_id

# The message header that names the queuing domain: its site_domain id, or None for the default domain
ACTING_HEADER = 'piefed_acting_domain'


def _queuing_domain_id() -> int | None:
    if not has_app_context():   # nothing to read the domain from (no task is queued so; the default then)
        return None
    domain = acting_domain()
    return None if domain.is_default else domain.id


class ActingTask(Task):
    def apply_async(self, args=None, kwargs=None, task_id=None, producer=None, link=None, link_error=None,
                    shadow=None, **options):
        headers = dict(options.pop('headers', None) or {})
        headers.setdefault(ACTING_HEADER, _queuing_domain_id())
        return super().apply_async(args, kwargs, task_id, producer, link, link_error, shadow, headers=headers,
                                   **options)


def queued_domain_id(request) -> int | None:
    """The domain id a task's request carries: a worker's message headers are attributes of its request, an eager
    run's are in `request.headers`."""
    value = getattr(request, ACTING_HEADER, None)
    if value is None:
        value = (getattr(request, 'headers', None) or {}).get(ACTING_HEADER)
    return value if isinstance(value, int) else None


# task id -> the acting_for block its run is in
_runs: dict[str, ExitStack] = {}


@task_prerun.connect
def _act_as_the_queuing_domain(task_id=None, task=None, **_kwargs):
    """The queuing domain (the default when the message names none, or a domain since removed; `acting_for` maps a
    local-only one to the default)."""
    stack = ExitStack()
    stack.enter_context(acting_for(domain_by_id(queued_domain_id(task.request))))
    _runs[task_id] = stack


@task_postrun.connect
def _stop_acting(task_id=None, **_kwargs):
    stack = _runs.pop(task_id, None)
    if stack is not None:
        stack.close()
