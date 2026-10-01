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
before; they were listed as strict xfails and are all fixed the same way, each
importer taking the module and looking the name up when it is called.

The blueprint list missed the service layer: `app.shared.post` (and community, feed,
user) reaches every blueprint through `app.activitypub`, whose routes then import
names from the half-loaded shared module, and `app.api.alpha` did the same through
`app.community.routes`. tests/conftest.py no longer primes anything, so
tests/test_shared_post_lifecycle.py run alone failed at collection. Every module
under app/ is now probed, not a hand-kept list.
"""
import os
import pathlib
import subprocess
import sys

import pytest

# Every cycle the probe found is fixed (owner ruling: post.routes, user.routes,
# auth.util, chat.util, feed.routes, topic.routes). A new one is a plain failure.
STILL_CIRCULAR = {}

APP = pathlib.Path(__file__).resolve().parent.parent / 'app'

MODULES = sorted(
    '.'.join(('app',) + path.relative_to(APP).with_suffix('').parts).removesuffix('.__init__')
    for path in APP.rglob('*.py')
    if '__pycache__' not in path.parts
)


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
