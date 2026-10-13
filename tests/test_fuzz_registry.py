"""The fuzz registry's completeness gate.

tests/fuzz/surfaces.py says which production functions are external input
entry points; tests.fuzz.targets.REGISTRY says which fuzz targets exist;
tests/fuzz/corpus/ holds what each target replays. Each of those can drift from
the others without anything failing -- a renamed function leaves the manifest
describing code that is gone, a surface module nobody wired in is a set of
targets nothing runs, a corpus directory whose target was renamed is replayed by
nobody -- so this file ties them together.

Also checked here, because the replay and the campaign are only as good as the
registry they read: the campaign's TARGETS IS the registry, and its argv puts
the defaults where an explicit flag can override them.

run_campaign itself is never imported here or by any collected test: importing
it means importing atheris and entering atheris.instrument_imports() inside the
coverage run. Its plain-Python half is tests.fuzz.campaign, which is what these
tests exercise; run_campaign.py is only read as source, and
test_no_collected_test_imports_run_campaign_or_instruments keeps it that way
for every other test module too.
"""

import ast
import importlib
import pathlib
import pkgutil
import types

import pytest

import tests.fuzz.targets as targets_package
from tests.fuzz.surfaces import SURFACES
from tests.fuzz.targets import REGISTRY, SURFACE_MODULES, Target, collect
from tests.fuzz import campaign

TESTS_ROOT = pathlib.Path(__file__).parent
CORPUS_ROOT = TESTS_ROOT / 'fuzz' / 'corpus'
RUN_CAMPAIGN = TESTS_ROOT / 'fuzz' / 'run_campaign.py'


def resolve(dotted: str):
    """Import the longest importable module prefix of `dotted`, then walk the
    rest as attributes. 'app.utils.allowlist_html' and
    'app.activitypub.signature.HttpSignature.parse_signature' both resolve."""
    parts = dotted.split('.')
    for split in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module('.'.join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for attribute in parts[split:]:
            obj = getattr(obj, attribute)
        return obj
    raise ModuleNotFoundError(dotted)


SURFACE_ENTRIES = sorted(
    (surface, dotted) for surface, names in SURFACES.items() for dotted in names)


@pytest.mark.parametrize('surface,dotted', SURFACE_ENTRIES,
                         ids=[f'{s}:{d}' for s, d in SURFACE_ENTRIES])
def test_every_surface_entry_is_an_importable_callable(surface, dotted):
    assert callable(resolve(dotted)), f'{dotted} ({surface}) is not callable'


@pytest.mark.parametrize('surface', sorted(SURFACES))
def test_every_surface_has_a_target(surface):
    assert any(target.surface == surface for target in REGISTRY.values()), (
        f'surface {surface!r} is listed in tests/fuzz/surfaces.py but no target fuzzes it')


@pytest.mark.parametrize('name', sorted(REGISTRY))
def test_every_target_names_a_listed_surface(name):
    assert REGISTRY[name].surface in SURFACES, (
        f'target {name!r} claims surface {REGISTRY[name].surface!r}, '
        f'which tests/fuzz/surfaces.py does not list')


@pytest.mark.parametrize('name', sorted(REGISTRY))
def test_every_target_is_registered_under_its_own_name(name):
    assert REGISTRY[name].name == name


@pytest.mark.parametrize('name', sorted(REGISTRY))
def test_every_target_has_a_seeded_corpus(name):
    """A target whose corpus directory vanished would otherwise silently stop
    being replayed, since parametrize over an empty glob collects nothing.

    (Moved here from tests/test_utils_fuzz_corpus.py with the registry.)"""
    directory = CORPUS_ROOT / name
    assert directory.is_dir(), f'{directory} is missing'
    assert any(p.is_file() for p in directory.iterdir()), f'{directory} is empty'


def test_every_corpus_directory_belongs_to_a_target():
    """An orphaned corpus -- its target renamed or deleted -- is replayed by
    nobody, so the reproducers in it stop pinning anything."""
    directories = {p.name for p in CORPUS_ROOT.iterdir() if p.is_dir()}
    assert directories - set(REGISTRY) == set()


def test_every_surface_module_is_registered():
    """A module in tests/fuzz/targets/ that SURFACE_MODULES does not list
    contributes nothing to REGISTRY, so its targets would never replay or run.
    Underscore modules (the Target record) are not surface modules."""
    on_disk = {info.name for info in pkgutil.iter_modules(targets_package.__path__)
               if not info.name.startswith('_')}
    registered = {module.__name__.rpartition('.')[2] for module in SURFACE_MODULES}
    assert on_disk == registered


def test_the_campaign_targets_are_the_registry():
    assert campaign.TARGETS is REGISTRY


def _is_instrument_imports_block(node) -> bool:
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == 'instrument_imports'
        for item in node.items)


def test_run_campaign_takes_its_targets_from_the_campaign_module():
    """Read as source, not imported (see the module docstring). TARGETS must be
    bound in exactly one place in run_campaign.py: imported from
    tests.fuzz.campaign inside an atheris.instrument_imports() block. Inside,
    because that import is what gets the checked functions instrumented; from
    campaign, because that is the name test_the_campaign_targets_are_the_registry
    proves is REGISTRY. A hand-maintained dict or a second binding fails."""
    tree = ast.parse(RUN_CAMPAIGN.read_text())
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
                (alias.asname or alias.name) == 'TARGETS' for alias in node.names):
            bindings.append(('import', node.module))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == 'TARGETS' for t in targets):
                bindings.append(('assign', None))
    assert bindings == [('import', 'tests.fuzz.campaign')]

    instrumented = [stmt for block in tree.body if _is_instrument_imports_block(block)
                    for stmt in block.body]
    assert any(isinstance(stmt, ast.ImportFrom) and stmt.module == 'tests.fuzz.campaign'
               and 'TARGETS' in {alias.name for alias in stmt.names}
               for stmt in instrumented), (
        'run_campaign.py must import TARGETS from tests.fuzz.campaign inside '
        'atheris.instrument_imports()')


# Calls that install atheris instrumentation or start libFuzzer. FuzzedDataProvider
# is deliberately not here: tests/test_link_parsers_fuzz.py uses it as a plain
# bytes decoder, which instruments nothing.
INSTRUMENTING_CALLS = {'instrument_imports', 'instrument_all', 'Fuzz'}


def instrumentation_violations(source: str) -> list[str]:
    """What in `source` imports tests.fuzz.run_campaign or calls an
    INSTRUMENTING_CALLS function. Parsed, so a docstring or a command line that
    merely names run_campaign (tests/test_fuzz_smoke.py has both) is not one."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f'import {a.name}' for a in node.names
                      if a.name == 'tests.fuzz.run_campaign']
        elif isinstance(node, ast.ImportFrom):
            if node.module == 'tests.fuzz.run_campaign':
                found.append(f'from {node.module} import ...')
            elif node.module == 'tests.fuzz' and any(a.name == 'run_campaign' for a in node.names):
                found.append('from tests.fuzz import run_campaign')
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            if name in INSTRUMENTING_CALLS:
                found.append(f'{name}() at line {node.lineno}')
    return found


COLLECTED_MODULES = sorted([*TESTS_ROOT.rglob('test_*.py'), *TESTS_ROOT.rglob('conftest.py')])


def test_no_collected_test_imports_run_campaign_or_instruments():
    """atheris instrumentation and coverage.py must never meet in the suite.
    Nothing collected may import run_campaign (whose import enters
    instrument_imports()) or call instrument_imports(), instrument_all() or
    Fuzz() itself; the campaign's testable logic is in tests.fuzz.campaign."""
    violations = {str(path.relative_to(TESTS_ROOT)): found
                  for path in COLLECTED_MODULES
                  if (found := instrumentation_violations(path.read_text()))}
    assert violations == {}


@pytest.mark.parametrize('source', [
    'import tests.fuzz.run_campaign',
    'from tests.fuzz import run_campaign',
    'from tests.fuzz.run_campaign import main',
    'import atheris\nwith atheris.instrument_imports():\n    pass',
    'import atheris\natheris.instrument_all()',
    'from atheris import Fuzz\nFuzz()',
])
def test_the_instrumentation_scan_catches(source):
    assert instrumentation_violations(source)


@pytest.mark.parametrize('source', [
    '"""python -m tests.fuzz.run_campaign x"""',
    "command = ['python', '-m', 'tests.fuzz.run_campaign']",
    'import atheris\natheris.FuzzedDataProvider(b"")',
    'from tests.fuzz import campaign',
])
def test_the_instrumentation_scan_ignores(source):
    assert not instrumentation_violations(source)


def test_a_duplicate_target_name_is_refused():
    def check(data):
        pass
    first = types.SimpleNamespace(__name__='first', TARGETS=[
        Target('dup', check, 'sanitizers', None, False)])
    second = types.SimpleNamespace(__name__='second', TARGETS=[
        Target('dup', check, 'sanitizers', None, False)])
    with pytest.raises(ValueError, match="'dup' is registered twice .again in second"):
        collect([first, second])


class TestCampaignArgv:
    """campaign.campaign_argv: the corpus layout, the -timeout default for
    budgeted targets, and an explicit flag beating a default."""

    @staticmethod
    def _target(budget_ms=None):
        return Target('t', lambda data: None, 'sanitizers', budget_ms, False)

    def test_layout_is_work_corpus_first_then_committed_then_artifacts(self):
        assert campaign.campaign_argv('prog', self._target(), ['-runs=5']) == [
            'prog', 'tests/fuzz/.work/t', 'tests/fuzz/corpus/t',
            '-artifact_prefix=tests/fuzz/corpus/t/', '-runs=5']

    def test_an_untimed_target_gets_no_timeout(self):
        argv = campaign.campaign_argv('prog', self._target(), [])
        assert not any(a.startswith('-timeout=') for a in argv)

    @pytest.mark.parametrize('budget_ms,seconds', [(50, 1), (100, 1), (101, 2), (2000, 20)])
    def test_a_budgeted_target_gets_ten_times_its_budget_rounded_up(self, budget_ms, seconds):
        argv = campaign.campaign_argv('prog', self._target(budget_ms), [])
        assert f'-timeout={seconds}' in argv

    def test_an_explicit_timeout_wins(self):
        argv = campaign.campaign_argv('prog', self._target(50), ['-timeout=30'])
        assert [a for a in argv if a.startswith('-timeout=')] == ['-timeout=30']

    def test_an_explicit_artifact_prefix_wins(self):
        argv = campaign.campaign_argv('prog', self._target(), ['-artifact_prefix=elsewhere/'])
        assert [a for a in argv if a.startswith('-artifact_prefix=')] == ['-artifact_prefix=elsewhere/']

    @pytest.mark.parametrize('argv', [['prog'], ['prog', 'no_such_target']])
    def test_a_missing_or_unknown_target_prints_usage(self, argv, capsys):
        assert campaign.select_target(argv) is None
        assert 'usage: prog <' in capsys.readouterr().out

    def test_a_known_target_is_selected(self):
        name = sorted(REGISTRY)[0]
        assert campaign.select_target(['prog', name, '-runs=1']) is REGISTRY[name]


def test_build_test_app_uses_the_test_config():
    """needs_app campaigns run inside this app; it must be the suite's TestConfig
    app (TESTING, the test SERVER_NAME), not a production-configured one."""
    application = campaign.build_test_app()
    assert application.config['TESTING'] is True
    assert application.config['SERVER_NAME'] == 'test.piefed.local'
