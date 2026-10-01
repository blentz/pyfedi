"""The overdue-cron warning on the admin dashboard.

Sub-project 120 -- `CronJobLog.get_frequency` in `app/models.py` and the loop in
`app/admin/routes.py` that reads it.

`get_frequency` maps a task's name to how often it should run, and it used to
fall off the end for a name it did not recognise. The caller compares
`diff_last_run > cron_task.get_frequency()`, so None there was

    TypeError: '>' not supported between instances of 'datetime.timedelta' and
    'NoneType'

on the first page an admin opens. `log_cron_task_to_db` writes its row with
`frequency` NULL, so adding or renaming a cron task broke that page, and the
failure landed nowhere near the change (D1334).

The fallback is a day. The test that matters more is the property below: every
name the application actually logs is listed in `get_frequency`, so a rename
fails here rather than quietly inheriting the fallback.
"""
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import CronJobLog, Role, Site, utcnow
from tests.factories import make_instance, make_user

# The seven names and their schedules, read off `get_frequency`'s own arms. Kept
# here as data so a change to one of them has to be made twice, deliberately.
KNOWN_SCHEDULES = {
    'send_missed_notifs': timedelta(hours=7),
    'process_email_bounces': timedelta(hours=7),
    'clean_up_old_activities': timedelta(hours=7),
    'remove_orphan_files': timedelta(days=8),
    'daily_maintenance_celery': timedelta(hours=25),
    'daily_maintenance': timedelta(hours=25),
    'send_queue': timedelta(minutes=5),
}

FALLBACK = timedelta(days=1)


@pytest.fixture
def env(app, db_session, site):
    from types import SimpleNamespace

    g.admin_ids = []
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    staffer = make_user(instance, 'staffer', local=True)
    staffer.verified = True
    role = Role.query.filter_by(name='Staff').first()
    if role is None:
        role = Role(name='Staff', weight=5)
        db.session.add(role)
        db.session.commit()
    staffer.roles.append(role)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(staffer.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client)


def dashboard(env):
    """The admin home, with its template stubbed: this is about the loop above
    the render, and the real template needs far more of a seeded instance."""
    with patch('app.admin.routes.render_template', return_value='rendered'):
        return env.client.get('/admin/')


def flashes(env):
    with patch('app.admin.routes.render_template', return_value='rendered'):
        env.client.get('/admin/')
    with env.client.session_transaction() as session:
        return [message for _category, message in session.get('_flashes', [])]


class TestHowOftenATaskShouldRun:
    @pytest.mark.parametrize('name,expected', sorted(KNOWN_SCHEDULES.items()))
    def test_a_name_it_knows(self, name, expected):
        assert CronJobLog(name=name).get_frequency() == expected

    def test_a_name_it_does_not_know(self):
        """D1334. This answered None, and the caller compares it with `>`."""
        assert CronJobLog(name='a_new_task').get_frequency() == FALLBACK

    @pytest.mark.parametrize('name', ['', None, 'SEND_QUEUE', 'send_queue ',
                                      'send_queu', 'daily_maintenance_x'])
    def test_other_names_it_does_not_know(self, name):
        assert CronJobLog(name=name).get_frequency() == FALLBACK

    def test_an_explicit_frequency_wins(self):
        assert CronJobLog(name='send_queue',
                          frequency=timedelta(hours=2)).get_frequency() == \
            timedelta(hours=2)

    def test_an_explicit_frequency_wins_for_an_unknown_name_too(self):
        assert CronJobLog(name='a_new_task',
                          frequency=timedelta(minutes=1)).get_frequency() == \
            timedelta(minutes=1)

    def test_it_always_answers_something_comparable(self):
        """The property the caller needs: `timedelta > X` must not raise."""
        for name in list(KNOWN_SCHEDULES) + ['a_new_task', '', None]:
            assert timedelta(seconds=1) > CronJobLog(name=name).get_frequency() \
                or timedelta(days=99) > CronJobLog(name=name).get_frequency()


class TestEveryTaskTheApplicationLogsIsListed:
    """The property that keeps the fallback from being load-bearing.

    `log_cron_task_to_db("x")` writes a row with `frequency` NULL, so `x` has to
    be a name `get_frequency` knows or the dashboard silently watches it on a
    one-day schedule it was never given.
    """

    def logged_names(self):
        pattern = re.compile(r'log_cron_task_to_db\(\s*["\']([a-z_]+)["\']')
        names = set()
        for path in sorted(Path('app').rglob('*.py')):
            names.update(pattern.findall(path.read_text(encoding='utf8')))
        return names

    def test_the_sweep_finds_the_call_sites(self):
        assert len(self.logged_names()) >= 7

    def test_every_logged_name_has_a_declared_schedule(self):
        unlisted = sorted(name for name in self.logged_names()
                          if CronJobLog(name=name).get_frequency() == FALLBACK)
        assert unlisted == [], \
            ('these cron tasks are logged but have no schedule in '
             f'CronJobLog.get_frequency: {unlisted}')

    def test_the_schedules_in_this_file_match_the_model(self):
        """If a schedule changes in the model, it changes here too -- which is
        the point of holding them twice."""
        for name, expected in KNOWN_SCHEDULES.items():
            assert CronJobLog(name=name).get_frequency() == expected, name


class TestTheDashboardsWarning:
    def test_no_cron_rows_at_all(self, env):
        assert dashboard(env).status_code == 200

    def test_a_task_that_ran_just_now_is_not_reported(self, env):
        db.session.add(CronJobLog(name='send_queue', last_run=utcnow()))
        db.session.commit()
        assert 'have not been run recently' not in ' '.join(flashes(env))

    def test_a_task_that_is_overdue_is_reported(self, env):
        db.session.add(CronJobLog(name='send_queue',
                                  last_run=utcnow() - timedelta(days=1)))
        db.session.commit()
        warning = ' '.join(flashes(env))
        assert 'have not been run recently' in warning
        assert 'send_queue' in warning

    def test_the_catalogue_is_asked_for_a_fixed_message(self, env):
        """D910, fixed: the overdue-task list was interpolated into the string
        before gettext saw it, so the catalogue was asked for a message holding
        this instance's task names and could never match. The msgid is fixed
        now and the list is passed as a parameter."""
        db.session.add(CronJobLog(name='send_queue',
                                  last_run=utcnow() - timedelta(days=1)))
        db.session.commit()
        asked = []

        def recording_gettext(message, **params):
            asked.append(message)
            return message % params if params else message

        with patch('app.admin.routes._', side_effect=recording_gettext):
            warning = ' '.join(flashes(env))

        assert 'Some cron tasks have not been run recently: %(tasks)s' in asked
        assert 'send_queue' in warning

    def test_a_task_with_a_name_the_model_does_not_know(self, env):
        """D1334 itself: this was a 500, and the page carries the host's load
        averages, disk usage and plugin list, so the admin lost all of it."""
        db.session.add(CronJobLog(name='a_new_task', last_run=utcnow()))
        db.session.commit()
        assert dashboard(env).status_code == 200

    def test_such_a_task_is_still_watched(self, env):
        """The fallback is a schedule, not a shrug."""
        db.session.add(CronJobLog(name='a_new_task',
                                  last_run=utcnow() - timedelta(days=2)))
        db.session.commit()
        assert 'a_new_task' in ' '.join(flashes(env))

    def test_several_overdue_tasks_are_all_named(self, env):
        db.session.add(CronJobLog(name='send_queue',
                                  last_run=utcnow() - timedelta(days=1)))
        db.session.add(CronJobLog(name='daily_maintenance',
                                  last_run=utcnow() - timedelta(days=3)))
        db.session.commit()
        warning = ' '.join(flashes(env))
        assert 'send_queue' in warning
        assert 'daily_maintenance' in warning

    def test_a_fresh_task_beside_an_overdue_one_is_not_named(self, env):
        db.session.add(CronJobLog(name='send_queue', last_run=utcnow()))
        db.session.add(CronJobLog(name='daily_maintenance',
                                  last_run=utcnow() - timedelta(days=3)))
        db.session.commit()
        warning = ' '.join(flashes(env))
        assert 'daily_maintenance' in warning
        assert 'send_queue' not in warning

    def test_an_explicit_frequency_decides_whether_it_is_late(self, env):
        """`frequency` is what an operator sets to override the model's list."""
        db.session.add(CronJobLog(name='send_queue',
                                  frequency=timedelta(days=7),
                                  last_run=utcnow() - timedelta(days=1)))
        db.session.commit()
        assert 'send_queue' not in ' '.join(flashes(env))


class TestWhatTheWriterLeavesOut:
    def test_a_logged_task_has_no_frequency_of_its_own(self, env):
        """Why `get_frequency`'s list exists at all, and why a None from it
        reached the comparison."""
        from app.utils import log_cron_task_to_db

        log_cron_task_to_db('send_queue')
        row = CronJobLog.query.filter_by(name='send_queue').one()
        assert row.frequency is None
        assert row.last_run is not None

    def test_logging_it_twice_moves_the_timestamp(self, env):
        from app.utils import log_cron_task_to_db

        log_cron_task_to_db('send_queue')
        first = CronJobLog.query.filter_by(name='send_queue').one().last_run
        log_cron_task_to_db('send_queue')
        db.session.expire_all()
        assert CronJobLog.query.filter_by(name='send_queue').count() == 1
        assert CronJobLog.query.filter_by(name='send_queue').one().last_run >= first

    def test_a_row_written_with_no_last_run_gets_one_anyway(self, env):
        """The dashboard's `if cron_task.last_run else 'never'` suggests None is
        possible; it is not through the ORM, because `last_run` has
        `default=utcnow` and SQLAlchemy applies a column default when the value
        is None at flush. Recorded rather than repaired: the guard is harmless
        and the alternative is inventing a case nothing produces."""
        db.session.add(CronJobLog(name='send_queue', last_run=None))
        db.session.commit()
        assert CronJobLog.query.filter_by(name='send_queue').one().last_run \
            is not None
