"""Group C's identity phases in `app/shared/tasks/maintenance.py`.

Sub-projects 29, 30, 31 and 32 closed the rest of this module. This file
covers what was left: the two blocks of `monitor_healthy_instances` gated on
`instance.software`, plus the task's own outer handler.

  Lemmy/PieFed admin roles and custom emoji  `:637-691`
  MBIN admin roles                           `:698-733`
  the task's outer handler                   `:736-738`

ENTRY IS GATED ON `software`. `:637` needs 'lemmy', 'piefed' or 'pylova';
`:698` needs 'mbin'. Sub-project 32's fixtures used `make_instance`'s
'mastodon' default, so neither block ran and both `if` statements were
covered only on their false arms. Every fixture here sets a matching value --
which means a test that sets `software` and forgets about these blocks enters
them anyway. Sub-project 32 learned that when a version test entered the
Lemmy block by accident and crashed.

THE HTTP HALF RUNS FIRST. An instance with `software` set still goes through
discovery and the fetch block above. `_quiet_http_half` neutralises them so a
test observes only the identity phases.

THE OUTER HANDLER IS REACHED THROUGH `:547`. `instance_banned` is called at
loop level, outside every `try`, so a raise there is the one failure that
reaches `:736` rather than being caught per-instance.

`get_request` RAISES, unlike `get_request_instance` which returns a synthetic
500. Task 1 probed what that means for a test that forgets to patch it:
an unpatched `get_request` raises inside the `try` at `:638`, and because
`response` is never assigned before that point in this iteration, the
`finally`'s `if response:` at `:689` raises `UnboundLocalError` before the
`except Exception` at `:685` can absorb anything. The task-level `except`
at `:736` then re-raises that `UnboundLocalError` to the caller -- a forgotten
patch kills the task rather than quietly redirecting it.
"""

from datetime import timedelta

import httpx
import pytest

from app import db
from app.models import Emoji, Instance, InstanceRole, User, utcnow
from app.shared.tasks.maintenance import monitor_healthy_instances
from tests.factories import make_instance, make_user


class _Recorder:
    """`calls` holds one `(args, kwargs)` tuple per invocation.

    So `c[0][0]` is the first positional argument. Same shape as the other
    maintenance test files' recorders; kept identical so a reader moving
    between them does not have to re-learn it.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _response(status_code=200, payload=None):
    """A real `httpx.Response`, because production calls `.json()` and `.close()`."""
    if payload is None:
        return httpx.Response(status_code=status_code)
    return httpx.Response(status_code=status_code, json=payload)


def _seed_instance(domain, software='mastodon'):
    """Seed an instance the task will actually see.

    `monitor_healthy_instances:543` filters `Instance.id != 1`, and the
    `db_session` teardown resets every sequence with
    `SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the FIRST
    instance a test seeds lands on exactly the id the task excludes.

    The reserved row absorbs that id and is excluded by STATE rather than by
    id -- dormant and gone_forever, with `start_trying_again` a year out -- so
    it stays invisible wherever it actually lands.
    """
    if db.session.query(Instance).filter_by(domain='reserved-id-one.example').first() is None:
        reserved = Instance(domain='reserved-id-one.example', software='mastodon')
        reserved.dormant = True
        reserved.gone_forever = True
        reserved.start_trying_again = utcnow() + timedelta(days=365)
        db.session.add(reserved)
        db.session.commit()
    return make_instance(domain, software=software)


def _quiet_http_half(monkeypatch):
    """Neutralise the fetch and discovery blocks above the identity phases.

    Every instance here has `software` set, so the HTTP half runs first. A 404
    from `get_request_instance` leaves the instance online -- two failure
    increments, well under `:609`'s threshold of 5 -- and drives no state the
    identity assertions read.
    """
    monkeypatch.setattr(
        'app.shared.tasks.maintenance.get_request_instance',
        lambda *args, **kwargs: _response(404))


def _site_payload(*actor_ids, emojis=None):
    """A Lemmy `/api/v3/site` body: admins, and optionally custom emoji.

    `:645` reads `admin['person']['actor_id']`; `:668` reads
    `emoji['custom_emoji']` and `emoji['keywords']`.
    """
    return {
        'admins': [{'person': {'actor_id': a}} for a in actor_ids],
        'custom_emojis': emojis if emojis is not None else [],
    }


def _emoji(shortcode, url='https://peer.example/e.png', category='cat', keywords=('happy',)):
    """One entry for `_site_payload`'s `custom_emojis` list."""
    return {
        'custom_emoji': {'shortcode': shortcode, 'image_url': url, 'category': category},
        'keywords': [{'keyword': k} for k in keywords],
    }


def _mbin_payload(*items):
    """An MBIN `/api/users/admins` body. `:705` reads `instance_data['items']`."""
    return {'items': list(items)}


class TestTheTaskLevelHandler:
    """`monitor_healthy_instances:736-738` -- the task's own `except`.

    `:739`'s `finally` and `:740`'s `session.close()` were already covered:
    every call reaches them. `:736-738` had never run, because every failure
    inside the loop is caught per-instance. `:547`'s `instance_banned` call is
    the exception -- it sits at loop level, outside every `try`, so a raise
    there is the one that reaches the task handler.
    """

    def test_a_raising_ban_check_rolls_back_and_re_raises(self, db_session, monkeypatch):
        """`:738`'s `raise`. The task does not swallow -- Celery must see it."""
        def _boom(domain):
            raise RuntimeError('ban check exploded')

        monkeypatch.setattr('app.shared.tasks.maintenance.instance_banned', _boom)
        _quiet_http_half(monkeypatch)
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        with pytest.raises(RuntimeError, match='ban check exploded'):
            monitor_healthy_instances()
