"""The interop stack and the unit-test stack are separate podman-compose projects.

podman-compose names a project after the directory unless the compose file names it. Both compose files sit in the
repository root, so both stacks were the project `pyfedi`, in one pod: `run_tests.sh`'s teardown removed a running
interop stack's containers, and the reverse. compose.interop.yaml names its own project."""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _project(compose_file):
    return yaml.safe_load((ROOT / compose_file).read_text()).get('name') or ROOT.name


def test_the_interop_stack_is_its_own_project():
    assert _project('compose.interop.yaml') == 'pyfedi-interop'
    assert _project('compose.interop.yaml') != _project('compose.test.yaml')


def test_conformance_runner_is_in_the_interop_project():
    services = yaml.safe_load((ROOT / 'compose.interop.yaml').read_text())['services']
    runner = services['conformance-runner']
    assert runner['profiles'] == ['conformance']
    assert runner['networks'] == services['interop-runner']['networks']


def test_conformance_run_script_fails_without_a_webfinger_transcript():
    script = (ROOT / 'interop/conformance/run.sh').read_text()
    feditest = re.search(r'feditest run (?:[^\n]*\\\n)*[^\n]*', script).group()   # the first feditest command, continuations and all
    assert 'webfinger.testplan.json' in feditest
    assert '</dev/null' in feditest   # a plan regression fails instead of waiting on a prompt
    assert '|| true' in feditest
    assert '[ -s results/webfinger.json ]' in script


def test_conformance_run_script_runs_from_its_own_directory():
    """The script's paths are relative to interop/conformance, so it moves there itself instead of assuming the caller
    stands in the repository root."""
    script = (ROOT / 'interop/conformance/run.sh').read_text()
    assert 'cd "$(dirname "$0")"' in script
    assert 'cd interop/conformance' not in script
