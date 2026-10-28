"""A Celery worker builds the app in its main process (celery_worker_docker.py) and then forks its pool. A database
connection the main process opened while building it is inherited by every child, which then share one socket: the
interop stack's piefed-celery logged `lost synchronization with server` on the first task a child ran, and one
such failure lost the Accept of a Follow, so the follower never saw itself accepted."""
from celery.signals import worker_init, worker_process_init
from sqlalchemy import text

from app import db


def test_the_main_process_holds_no_database_connection_when_the_pool_forks(app):
    with app.app_context():
        db.session.execute(text('SELECT 1'))
        db.session.commit()
        assert db.engine.pool.checkedin() >= 1   # the connection building the app left behind

        worker_init.send(sender=None)

        assert (db.engine.pool.checkedin(), db.engine.pool.checkedout()) == (0, 0)


def test_a_forked_child_discards_the_pooled_connections_it_inherited(app):
    """A child re-forked later (--max-tasks-per-child) comes from a main process that may have reconnected."""
    with app.app_context():
        db.session.execute(text('SELECT 1'))
        db.session.commit()
        assert db.engine.pool.checkedin() >= 1

        worker_process_init.send(sender=None)

        assert (db.engine.pool.checkedin(), db.engine.pool.checkedout()) == (0, 0)
