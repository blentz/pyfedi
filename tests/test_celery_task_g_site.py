"""A Celery worker pushes one app context for its whole life (celery_worker_docker.py), so `g` outlives each task.

A task that set `g.site` from its own session (get_task_session) left the next task a Site whose session had
committed and closed: expired and detached. Reading any attribute of it raised DetachedInstanceError -- measured on
hell.cloud as 'Instance <Site at 0x...> is not bound to a Session' for 5-30 Creates an hour since 2026-10-10, and
for every relayed robot.villas post, since a post with no language falls back to site_language_id()."""
import pytest
from celery.signals import task_prerun
from flask import g
from sqlalchemy.orm.exc import DetachedInstanceError

from app.models import Site
from app.utils import get_task_session, site_language_id

pytestmark = pytest.mark.usefixtures('site')


def _site_left_by_an_earlier_task():
    session = get_task_session()
    site = session.get(Site, 1)
    session.commit()   # expires it, as every task's commit does
    session.close()    # and detaches it
    return site


def test_a_site_left_in_g_by_an_earlier_task_cannot_be_read(app):
    """The failure itself, so the test below is known to start from it."""
    with app.app_context():
        g.site = _site_left_by_an_earlier_task()
        with pytest.raises(DetachedInstanceError):
            site_language_id()


def test_each_task_starts_without_the_previous_tasks_site(app):
    with app.app_context():
        g.site = _site_left_by_an_earlier_task()

        task_prerun.send(sender=None, task_id='next-task', task=None, args=(), kwargs={})

        assert 'site' not in g
        site_language_id()   # falls back to a lookup in the live session instead of raising


def test_a_task_run_eagerly_inside_a_request_keeps_the_requests_site(app):
    """An eager task (tests, `apply()`) runs inside its caller's context, whose g.site is live and still needed."""
    from app import celery

    @celery.task
    def _noop():
        return 'ran'

    with app.test_request_context():
        g.site = object()
        assert _noop.apply().get() == 'ran'
        assert 'site' in g


def test_a_task_outside_any_app_context_starts_without_error(monkeypatch):
    """A process with no app context pushed has no g to clear."""
    import app as app_package
    monkeypatch.setattr(app_package, 'has_app_context', lambda: False)

    app_package._forget_the_previous_tasks_site(task=None)
