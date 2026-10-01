"""Every blueprint module imports cleanly in a fresh interpreter, whichever is first.

U-circular-import, fixed. `app.community.routes` imports `app.activitypub.signature`,
which runs `app/activitypub/__init__.py` and so `app.activitypub.routes`, which did
`from app.community.routes import show_community` -- while `app.community.routes`
was still mid-import, with `show_community` not yet defined. Whichever package
started first won: importing `app.community` (or anything that reaches it first,
such as `app.instance.util`) before `app.activitypub` raised ImportError.
`app.activitypub.routes` now imports the module and looks the function up when it
is called.

tests/conftest.py primes `app.activitypub.signature` for the session, which hides
every such cycle from the rest of the suite, so these rows import each module in a
fresh interpreter instead. The same probe found six MORE cycles, not registered
before; they are listed as strict xfails, so fixing one turns its row red until the
mark is removed.
"""
import os
import subprocess
import sys

import pytest

STILL_CIRCULAR = {
    'app.auth.routes': 'auth.util <-> random_token', 'app.auth.util': 'auth.util <-> random_token',
    'app.chat.routes': 'chat.util <-> send_message', 'app.chat.util': 'chat.util <-> send_message',
    'app.feed.routes': 'feed.routes <-> get_all_child_feed_ids',
    'app.feed.util': 'feed.routes <-> get_all_child_feed_ids',
    'app.post.routes': 'post.routes <-> continue_discussion',
    'app.post.util': 'post.routes <-> continue_discussion',
    'app.tag.routes': 'topic.routes <-> get_all_child_topic_ids',
    'app.topic.routes': 'topic.routes <-> get_all_child_topic_ids',
    'app.user.routes': 'user.routes <-> show_profile',
}

MODULES = [
    f'app.{package}.{name}'
    for package, names in (
        ('activitypub', ('routes', 'util')), ('admin', ('routes', 'util')),
        ('auth', ('routes', 'util')), ('chat', ('routes', 'util')),
        ('community', ('routes', 'util')), ('dev', ('routes',)), ('domain', ('routes',)),
        ('feed', ('routes', 'util')), ('instance', ('routes', 'util')),
        ('main', ('routes', 'util')), ('post', ('routes', 'util')), ('search', ('routes',)),
        ('tag', ('routes',)), ('topic', ('routes',)), ('user', ('routes',)),
    )
    for name in names
]


@pytest.mark.parametrize('module', [
    pytest.param(module, marks=pytest.mark.xfail(strict=True, reason=STILL_CIRCULAR[module]))
    if module in STILL_CIRCULAR else module
    for module in MODULES
])
def test_the_module_imports_first_in_a_fresh_interpreter(module):
    result = subprocess.run([sys.executable, '-c', f'import {module}'],
                            capture_output=True, text=True, env=os.environ.copy(),
                            timeout=120)

    assert result.returncode == 0, result.stderr[-2000:]
