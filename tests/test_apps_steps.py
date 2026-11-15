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


def test_a_failure_is_tried_again_from_fresh_and_passes(results, tmp_path):
    calls = []

    def attempt(retry):
        calls.append(retry)
        if not retry:
            (tmp_path / 'a-view.png').write_bytes(b'png')

            def view():
                raise AssertionError('slow screen')
            return {'login': lambda: 'in', 'view': view, 'post': lambda: None}, lambda step: None
        return {'login': lambda: 'in', 'view': lambda: 'seen', 'post': lambda: 'posted'}, lambda step: None
    steps.run_with_retry('a', {}, attempt, version='1', results_dir=tmp_path)
    assert calls == [False, True]
    assert results('a') == {'login': 'passed', 'view': 'passed', 'post': 'passed'}
    data = json.loads((tmp_path / 'a.json').read_text())
    assert data['retried'] == {'view': 'AssertionError: slow screen'} and data['version'] == '1'
    assert (tmp_path / 'a-try1-view.png').read_bytes() == b'png' and not (tmp_path / 'a-view.png').exists()


def test_a_second_failure_is_the_result_and_the_first_is_kept(results, tmp_path):
    def attempt(retry):
        def view():
            raise AssertionError('again' if retry else 'first')
        return {'login': lambda: None, 'view': view, 'post': lambda: None}, None
    steps.run_with_retry('a', {}, attempt, results_dir=tmp_path)
    data = json.loads((tmp_path / 'a.json').read_text())
    assert results('a') == {'login': 'passed', 'view': 'failed', 'post': 'blocked'}
    assert data['steps']['view']['detail'] == 'AssertionError: again' and data['retried'] == {'view': 'AssertionError: first'}


def test_a_clean_run_is_not_tried_again(results, tmp_path):
    calls = []

    def attempt(retry):
        calls.append(retry)
        return {'login': lambda: None, 'view': lambda: None, 'post': lambda: None}, None
    steps.run_with_retry('a', {}, attempt, results_dir=tmp_path)
    assert calls == [False] and 'retried' not in json.loads((tmp_path / 'a.json').read_text())


def test_an_expected_failure_is_not_tried_again(results, tmp_path):
    """The matrix knows this step fails in this app: running it again would only take time."""
    calls = []
    app = {'expect': {'post': {'outcome': 'failed', 'reason': 'the app cannot'}}}

    def attempt(retry):
        calls.append(retry)

        def post():
            raise AssertionError('as known')
        return {'login': lambda: None, 'view': lambda: None, 'post': post}, None
    steps.run_with_retry('a', app, attempt, results_dir=tmp_path)
    assert calls == [False]
