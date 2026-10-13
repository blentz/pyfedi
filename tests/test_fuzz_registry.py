"""The fuzz registry's completeness gate.

tests/fuzz/surfaces.py says which production functions are external input
entry points; tests.fuzz.targets.REGISTRY says which fuzz targets exist;
tests/fuzz/corpus/ holds what each target replays. Each of those can drift from
the others without anything failing -- a renamed function leaves the manifest
describing code that is gone, a surface module nobody wired in is a set of
targets nothing runs, a corpus directory whose target was renamed is replayed by
nobody -- so this file ties them together.

Also checked here, because the replay and the campaign are only as good as the
registry they read: run_campaign.TARGETS IS the registry, and run_campaign's
argv puts the defaults where an explicit flag can override them.

Importing run_campaign imports atheris and enters atheris.instrument_imports(),
which is harmless here only because tests.fuzz.targets is imported first: see
the comment under that with block in run_campaign.py.
"""

import importlib
import pathlib
import pkgutil
import types

import pytest

import tests.fuzz.targets as targets_package
from tests.fuzz.surfaces import SURFACES
from tests.fuzz.targets import REGISTRY, SURFACE_MODULES, Target, collect
from tests.fuzz import run_campaign  # after tests.fuzz.targets: see the module docstring

CORPUS_ROOT = pathlib.Path(__file__).parent / 'fuzz' / 'corpus'


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


def test_run_campaign_targets_is_the_registry():
    assert run_campaign.TARGETS is REGISTRY


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
    """run_campaign.campaign_argv: the corpus layout, the -timeout default for
    budgeted targets, and an explicit flag beating a default."""

    @staticmethod
    def _target(budget_ms=None):
        return Target('t', lambda data: None, 'sanitizers', budget_ms, False)

    def test_layout_is_work_corpus_first_then_committed_then_artifacts(self):
        assert run_campaign.campaign_argv('prog', self._target(), ['-runs=5']) == [
            'prog', 'tests/fuzz/.work/t', 'tests/fuzz/corpus/t',
            '-artifact_prefix=tests/fuzz/corpus/t/', '-runs=5']

    def test_an_untimed_target_gets_no_timeout(self):
        argv = run_campaign.campaign_argv('prog', self._target(), [])
        assert not any(a.startswith('-timeout=') for a in argv)

    @pytest.mark.parametrize('budget_ms,seconds', [(50, 1), (100, 1), (101, 2), (2000, 20)])
    def test_a_budgeted_target_gets_ten_times_its_budget_rounded_up(self, budget_ms, seconds):
        argv = run_campaign.campaign_argv('prog', self._target(budget_ms), [])
        assert f'-timeout={seconds}' in argv

    def test_an_explicit_timeout_wins(self):
        argv = run_campaign.campaign_argv('prog', self._target(50), ['-timeout=30'])
        assert [a for a in argv if a.startswith('-timeout=')] == ['-timeout=30']

    def test_an_explicit_artifact_prefix_wins(self):
        argv = run_campaign.campaign_argv('prog', self._target(), ['-artifact_prefix=elsewhere/'])
        assert [a for a in argv if a.startswith('-artifact_prefix=')] == ['-artifact_prefix=elsewhere/']

    def test_unknown_target_prints_usage(self, capsys):
        assert run_campaign.main(['prog', 'no_such_target']) == 2
        assert 'usage: prog <' in capsys.readouterr().out


def test_build_test_app_uses_the_test_config():
    """needs_app campaigns run inside this app; it must be the suite's TestConfig
    app (TESTING, the test SERVER_NAME), not a production-configured one."""
    application = run_campaign.build_test_app()
    assert application.config['TESTING'] is True
    assert application.config['SERVER_NAME'] == 'test.piefed.local'
