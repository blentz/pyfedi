"""interop/apps/steps.py: one app's steps run in order, each recorded."""
import json

import pytest

from interop.apps import steps


@pytest.fixture
def results(tmp_path):
    return lambda name: {step: s['outcome'] for step, s in json.loads((tmp_path / f'{name}.json').read_text())['steps'].items()}


def test_every_step_passes_with_its_detail(results, tmp_path):
    steps.run('a', {}, {'login': lambda: 'in', 'view': lambda: None, 'post': lambda: 'posted'}, version='1.2', results_dir=tmp_path)
    data = json.loads((tmp_path / 'a.json').read_text())
    assert data['version'] == '1.2'
    assert {s: v['detail'] for s, v in data['steps'].items()} == {'login': 'in', 'view': '', 'post': 'posted'}
    assert results('a') == {'login': 'passed', 'view': 'passed', 'post': 'passed'}


def test_a_failure_blocks_the_later_steps_and_captures_evidence(results, tmp_path):
    captured = []

    def view():
        raise AssertionError('no seeded post')
    steps.run('a', {}, {'login': lambda: None, 'view': view, 'post': lambda: None}, on_failure=captured.append, results_dir=tmp_path)
    assert results('a') == {'login': 'passed', 'view': 'failed', 'post': 'blocked'}
    assert captured == ['view']
    assert json.loads((tmp_path / 'a.json').read_text())['steps']['view']['detail'] == 'AssertionError: no seeded post'


def test_evidence_that_fails_does_not_hide_the_outcome(results, tmp_path):
    def boom(step):
        raise RuntimeError('no screen')

    def login():
        raise TimeoutError('slow')
    steps.run('a', {}, {'login': login}, on_failure=boom, results_dir=tmp_path)
    assert results('a') == {'login': 'failed', 'view': 'blocked', 'post': 'blocked'}


def test_inapplicable_steps_are_not_run(results, tmp_path):
    app = {'expect': {'login': {'outcome': 'inapplicable', 'reason': 'no accounts'}}}
    steps.run('a', app, {'view': lambda: None, 'post': lambda: None}, results_dir=tmp_path)
    assert results('a') == {'login': 'inapplicable', 'view': 'passed', 'post': 'passed'}


def test_an_unsupported_step_is_recorded_so_and_blocks_nothing(results, tmp_path):
    """A driven app whose step needs an API PieFed lacks yet (the Mastodon apps' timelines, plan W1.2)."""
    captured = []

    def view():
        raise steps.Unsupported('no timelines yet')
    steps.run('a', {}, {'login': lambda: None, 'view': view, 'post': lambda: 'posted'}, on_failure=captured.append,
              results_dir=tmp_path)
    assert results('a') == {'login': 'passed', 'view': 'unsupported', 'post': 'passed'}
    assert captured == []
    assert json.loads((tmp_path / 'a.json').read_text())['steps']['view']['detail'] == 'no timelines yet'
