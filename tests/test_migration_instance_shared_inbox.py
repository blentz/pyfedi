"""Migration e8b4d2a6c1f3: an instance already stored learns the shared inbox its stored actors name (P1)."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.models import Instance, SiteDomain
from tests.factories import make_instance, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
    'e8b4d2a6c1f3_instance_shared_inbox_backfill.py'
PIXELFED = 'pixelfed.example'
SHARED = f'https://{PIXELFED}/f/inbox'
GUESS = f'https://{PIXELFED}/inbox'


def load_migration():
    spec = importlib.util.spec_from_file_location('instance_shared_inbox_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance(domain=PIXELFED):
    instance = make_instance(domain, software='pixelfed')
    instance.inbox = f'https://{domain}/inbox'
    instance.shared_inbox = None
    db.session.commit()
    return instance


def _actor(instance, name, inbox, own_inbox=None):
    user = make_user(instance, name)
    user.ap_inbox_url = inbox
    user.ap_own_inbox_url = own_inbox
    db.session.commit()
    return user


def _upgrade():
    db.session.commit()
    with db.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            load_migration().upgrade()
    db.session.expire_all()


def _boxes(instance_id):
    instance = db.session.get(Instance, instance_id)
    return instance.inbox, instance.shared_inbox


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('e8b4d2a6c1f3', 'd5a9c3e7f1b4')


def test_a_shared_inbox_two_actors_name_becomes_the_instances(app, db_session):
    instance = _instance()
    _actor(instance, 'erin', SHARED)
    _actor(instance, 'finn', SHARED)

    _upgrade()

    assert _boxes(instance.id) == (SHARED, SHARED)


def test_one_actor_whose_own_inbox_differs_is_enough(app, db_session):
    instance = _instance()
    _actor(instance, 'erin', SHARED, own_inbox=f'https://{PIXELFED}/users/erin/inbox')

    _upgrade()

    assert _boxes(instance.id) == (SHARED, SHARED)


def test_one_actor_whose_inbox_may_be_its_own_teaches_nothing(app, db_session):
    instance = _instance()
    _actor(instance, 'erin', f'https://{PIXELFED}/users/erin/inbox')                # own inbox unknown
    _actor(instance, 'finn', f'https://{PIXELFED}/users/finn/inbox',
           own_inbox=f'https://{PIXELFED}/users/finn/inbox')                          # own inbox, no shared one

    _upgrade()

    assert _boxes(instance.id) == (GUESS, None)


def test_an_inbox_on_another_host_is_not_taken(app, db_session):
    instance = _instance()
    _actor(instance, 'erin', 'https://elsewhere.example/inbox')
    _actor(instance, 'finn', 'https://elsewhere.example/inbox')

    _upgrade()

    assert _boxes(instance.id) == (GUESS, None)


def test_the_most_used_inbox_wins(app, db_session):
    instance = _instance()
    for name in ('erin', 'finn', 'gale'):
        _actor(instance, name, SHARED)
    _actor(instance, 'hugo', f'https://{PIXELFED}/other-inbox')
    _actor(instance, 'ivy', f'https://{PIXELFED}/other-inbox')

    _upgrade()

    assert _boxes(instance.id) == (SHARED, SHARED)


def test_a_hosted_domains_instance_is_left_alone(app, db_session):
    db.session.add(SiteDomain(domain='hosted.example', is_default=False))   # makes its own instance row on insert
    db.session.commit()
    hosted_id = db.session.query(SiteDomain.instance_id).filter_by(domain='hosted.example').scalar()
    hosted = db.session.get(Instance, hosted_id)
    before = (hosted.inbox, hosted.shared_inbox)
    for name in ('erin', 'finn'):
        user = make_user(hosted, name)
        user.ap_inbox_url = f'https://{hosted.domain}/f/inbox'
    db.session.commit()

    _upgrade()

    assert _boxes(hosted_id) == before


def test_it_can_run_again(app, db_session):
    instance = _instance()
    _actor(instance, 'erin', SHARED)
    _actor(instance, 'finn', SHARED)

    _upgrade()
    _upgrade()

    assert _boxes(instance.id) == (SHARED, SHARED)
    with db.engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM instance WHERE domain = :d'), {'d': PIXELFED}).scalar() == 1
