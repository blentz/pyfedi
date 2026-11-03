"""`flask lemmy-import`: a Lemmy admin becomes an admin here, through the built-in Admin role.

The Lemmy database is stood in by an engine whose queries answer from a script: one per query the command makes, in
order, and no rows for any query past the script's end.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app

from app import cli, db
from app.models import Role, User
from tests.factories import admin_role, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def commands(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app.test_cli_runner()


def lemmy_person(person_id, name, *, admin, email='lemmy@example.com', instance_id=1):
    return SimpleNamespace(person_id=person_id, name=name, display_name=None, avatar=None, bio='', published=None,
                           updated=None, actor_id=f'https://lemmy.example/u/{name}', banned=False, deleted=False,
                           private_key=None, public_key=None, matrix_user_id=None, bot_account=False,
                           ban_expires=None, instance_id=instance_id, email=email,
                           password_encrypted='hash' if email else None, email_verified=True, admin=admin,
                           totp_2fa_secret=None)


class ScriptedLemmy:
    """An engine whose connection answers each query with the next list in `answers`, then with no rows."""

    def __init__(self, *answers):
        self.answers = list(answers)

    @contextmanager
    def connect(self):
        yield self

    def execute(self, _query):
        return self.answers.pop(0) if self.answers else []


def run_import(commands, *answers):
    lemmy = ScriptedLemmy([], *answers)   # the first query is the connection check's SELECT 1
    with patch('app.cli.create_engine', return_value=lemmy):
        result = commands.invoke(args=['lemmy-import'], input='postgresql://lemmy@localhost/lemmy\n')
    assert result.exception is None, result.output
    return result


def test_a_lemmy_admin_is_given_the_built_in_admin_role(commands):
    make_instance(current_app.config['SERVER_NAME'], software='piefed')
    make_user(None, 'founder', local=True)
    admin_role()
    db.session.commit()

    run_import(commands, [], [lemmy_person(50, 'lemmyadmin', admin=True), lemmy_person(51, 'lemmyuser', admin=False)])

    db.session.expire_all()
    assert db.session.get(User, 50).is_admin()
    assert not db.session.get(User, 51).is_admin()
    assert [role.name for role in db.session.get(User, 50).roles] == [Role.admin().name]
