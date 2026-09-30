"""User columns that code reads as plain values, and so must never be NULL.

Each column here was nullable with only a Python-side default, which the ORM
applies on INSERT and never to a row it did not write. A migration backfills
the NULLs and makes the column NOT NULL with a matching server default, so the
schema and the model agree whoever is writing.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import User
from tests.factories import make_instance, make_user


def test_user_verified_cannot_be_null(db_session):
    """D645, fixed. `verified` accepted NULL, and `user.verified is False`
    guards (the API entry gate among them) let such a user straight through.
    The column is now NOT NULL, backfilled to false."""
    user = make_user(make_instance('remote.example'), 'someone', local=True)
    user.verified = None

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_user_verified_defaults_to_false_in_the_database(db_session):
    """D645. The server default covers a write that bypasses the ORM."""
    assert User.__table__.c.verified.nullable is False
    verified = db.session.execute(text(
        'INSERT INTO "user" (user_name) VALUES (:name) RETURNING verified'),
        {'name': 'rawinsert'}).scalar()
    assert verified is False


def test_user_unread_notifications_cannot_be_null(db_session):
    """D274, fixed. The column was added nullable with no backfill, so every
    user row older than that migration held NULL and the ~50
    `unread_notifications += 1` sites raised TypeError on it. It is now NOT
    NULL, backfilled to 0."""
    user = make_user(make_instance('remote.example'), 'someone', local=True)
    user.unread_notifications = None

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_user_unread_notifications_defaults_to_zero_in_the_database(db_session):
    """D274. The server default covers a write that bypasses the ORM."""
    assert User.__table__.c.unread_notifications.nullable is False
    unread = db.session.execute(text(
        'INSERT INTO "user" (user_name) VALUES (:name) RETURNING unread_notifications'),
        {'name': 'rawinsert'}).scalar()
    assert unread == 0
