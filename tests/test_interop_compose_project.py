"""The interop stack and the unit-test stack are separate podman-compose projects.

podman-compose names a project after the directory unless the compose file names it. Both compose files sit in the
repository root, so both stacks were the project `pyfedi`, in one pod: `run_tests.sh`'s teardown removed a running
interop stack's containers, and the reverse. compose.interop.yaml names its own project."""
import pathlib

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
