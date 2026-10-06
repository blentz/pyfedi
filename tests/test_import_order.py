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

SPEED. Each row used to start its own interpreter, and 1.1s of every 1.6s import
was the same third-party packages (SQLAlchemy, marshmallow, alembic ...): about
five minutes for the file. A zygote process now imports every module that
`import app` pulls in EXCEPT app's own and config, once, then forks a child per
row; the child imports the row's module with no app module loaded, which is the
fresh-interpreter condition these rows test. Third-party packages never import
app, so having them loaded already cannot hide a cycle.
"""
import json
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
) + ['config']   # config.py imported app.constants, so app/__init__ (from config import Config) cycled


PROBE_TIMEOUT = 120

# Lists what importing every app module loads besides app's own modules and config.
# `import app` alone is not enough: create_app imports the blueprints inside a
# function, so their packages would still be imported once per row.
_PRELOAD_LISTER = r'''
import importlib, json, sys
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except BaseException:
        pass
print(json.dumps(sorted(n for n in sys.modules if n.split('.')[0] not in ('app', 'config'))))
'''

# Imports the preload list, says `ready`, then forks one child per module name read
# from stdin. The child imports that module; the parent answers with one JSON line.
_ZYGOTE = r'''
import importlib, json, os, signal, sys, traceback
for name in json.loads(sys.stdin.readline()):
    try:
        importlib.import_module(name)
    except BaseException:
        pass
print('ready', flush=True)
for line in sys.stdin:
    module = line.strip()
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        signal.alarm(%d)
        code = 0
        try:
            importlib.import_module(module)
        except BaseException:
            code = 1
            os.write(write_end, traceback.format_exc().encode())
        os.close(write_end)
        os._exit(code)
    os.close(write_end)
    stderr = b''
    while chunk := os.read(read_end, 65536):
        stderr += chunk
    os.close(read_end)
    _, status = os.waitpid(pid, 0)
    print(json.dumps({'code': os.waitstatus_to_exitcode(status),
                      'stderr': stderr.decode(errors='replace')}), flush=True)
''' % PROBE_TIMEOUT


def preload_list() -> list:
    result = subprocess.run([sys.executable, '-c', _PRELOAD_LISTER, json.dumps(MODULES)], capture_output=True,
                            text=True, env=os.environ.copy(), timeout=PROBE_TIMEOUT, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


class Zygote:
    def __init__(self, preload):
        self.process = subprocess.Popen([sys.executable, '-c', _ZYGOTE], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        self.process.stdin.write(json.dumps(preload) + '\n')
        self.process.stdin.flush()
        assert self.process.stdout.readline().strip() == 'ready'

    def probe(self, module) -> dict:
        self.process.stdin.write(module + '\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=PROBE_TIMEOUT)


@pytest.fixture(scope='module')
def zygote():
    started = Zygote(preload_list())
    yield started
    started.close()


def test_the_zygote_preloads_nothing_of_apps_own():
    assert not [name for name in preload_list() if name.split('.')[0] in ('app', 'config')]


def test_a_failing_import_is_reported_with_its_traceback(zygote):
    result = zygote.probe('app.no_such_module_here')

    assert result['code'] == 1
    assert 'ModuleNotFoundError' in result['stderr']


@pytest.mark.parametrize('module', [
    pytest.param(module, marks=pytest.mark.xfail(strict=True, reason=STILL_CIRCULAR[module]))
    if module in STILL_CIRCULAR else module
    for module in MODULES
])
def test_the_module_imports_first_in_a_fresh_interpreter(zygote, module):
    result = zygote.probe(module)

    assert result['code'] == 0, result['stderr'][-2000:]
