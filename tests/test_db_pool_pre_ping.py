"""A pooled connection the server dropped is replaced on checkout, not handed to a Celery task to fail with
"server closed the connection unexpectedly". (This does not explain why a connection died; it stops the task paying.)"""
from app import db


def test_the_engine_pings_a_pooled_connection_before_handing_it_out(app):
    with app.app_context():
        assert db.engine.pool._pre_ping is True
