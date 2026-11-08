"""The client-app gate (interop/apps/matrix.py) and the committed matrix."""
import json

import pytest

from interop.apps.matrix import MATRIX, STEPS, collect, compare, inapplicable, load_matrix, main, record, summary

UNSUPPORTED = {'outcome': 'unsupported', 'reason': 'no Mastodon client API'}
BLOCKED = {'outcome': 'blocked', 'reason': 'login is unsupported'}


def _result(app, version='1.0', **steps):
    return {'app': app, 'version': version,
            'steps': {step: {'outcome': steps.get(step, 'passed'), 'detail': ''} for step in STEPS}}


def test_a_passing_app_with_no_expectations_is_clean():
    assert compare({'voyager': {}}, {'voyager': _result('voyager')}) == []


def test_a_failure_is_named_with_its_detail():
    result = _result('voyager', post='failed')
    result['steps']['post']['detail'] = 'POST /api/alpha/post 500'
    assert compare({'voyager': {}}, {'voyager': result}) == ['voyager/post: failed, expected passed: POST /api/alpha/post 500']


def test_an_expected_gap_is_clean():
    matrix = {'tusky': {'expect': {'login': UNSUPPORTED, 'view': BLOCKED, 'post': BLOCKED}}}
    result = _result('tusky', login='unsupported', view='blocked', post='blocked')
    assert compare(matrix, {'tusky': result}) == []


def test_an_expected_gap_that_now_passes_asks_for_the_matrix_to_change():
    matrix = {'tusky': {'expect': {'login': UNSUPPORTED}}}
    assert compare(matrix, {'tusky': _result('tusky')}) == [
        'tusky/login: now passes, expected unsupported: remove the expectation from matrix.json']


def test_a_gap_that_turns_into_a_failure_is_named():
    matrix = {'tusky': {'expect': {'login': UNSUPPORTED}}}
    assert compare(matrix, {'tusky': _result('tusky', login='failed')}) == ['tusky/login: failed, expected unsupported']


def test_an_app_that_did_not_run_fails_the_gate():
    assert compare({'voyager': {}}, {}) == ['voyager: no result (the app did not run)']


def test_a_step_that_did_not_run_is_missing():
    result = _result('voyager')
    del result['steps']['post']
    assert compare({'voyager': {}}, {'voyager': result}) == ['voyager/post: missing, expected passed']


def test_a_result_for_an_unknown_app_fails_the_gate():
    assert compare({}, {'ghost': _result('ghost')}) == ['ghost: a result for an app matrix.json does not name']


@pytest.mark.parametrize('expect, problem', [
    ({'login': {'outcome': 'unsupported'}}, 'x/login: the expectation has no reason'),
    ({'login': {'outcome': 'passed', 'reason': 'r'}}, 'x/login: an expectation must be one of failed, unsupported, blocked, inapplicable'),
    ({'logout': UNSUPPORTED}, 'x/logout: not a step (login, view, post)'),
], ids=['no-reason', 'pass-expectation', 'unknown-step'])
def test_malformed_expectations_fail_the_gate(expect, problem):
    assert problem in compare({'x': {'expect': expect}}, {'x': _result('x')})


def test_inapplicable_steps_come_from_the_expectations():
    app = {'expect': {'login': {'outcome': 'inapplicable', 'reason': 'r'}, 'view': UNSUPPORTED}}
    assert inapplicable(app) == {'login'}
    assert inapplicable({}) == set()


def test_record_merges_steps_and_keeps_the_version(tmp_path):
    record('voyager', 'login', 'passed', version='2.40.0', results_dir=tmp_path)
    record('voyager', 'view', 'failed', detail='no posts', results_dir=tmp_path)
    result = json.loads((tmp_path / 'voyager.json').read_text())
    assert result['version'] == '2.40.0'
    assert result['steps'] == {'login': {'outcome': 'passed', 'detail': ''}, 'view': {'outcome': 'failed', 'detail': 'no posts'}}


def test_record_caps_the_detail(tmp_path):
    record('voyager', 'login', 'failed', detail='x' * 5000, results_dir=tmp_path)
    assert len(json.loads((tmp_path / 'voyager.json').read_text())['steps']['login']['detail']) == 2000


@pytest.mark.parametrize('step, outcome', [('logout', 'passed'), ('login', 'ok')], ids=['step', 'outcome'])
def test_record_refuses_unknown_names(tmp_path, step, outcome):
    with pytest.raises(ValueError):
        record('voyager', step, outcome, results_dir=tmp_path)


def test_collect_names_a_broken_result_file(tmp_path):
    (tmp_path / 'bad.json').write_text('{')
    (tmp_path / 'good.json').write_text(json.dumps(_result('good')))
    results, problems = collect(tmp_path)
    assert list(results) == ['good']
    assert problems == ['bad.json: not a result file']


def test_collect_without_a_results_dir_finds_nothing(tmp_path):
    assert collect(tmp_path / 'absent') == ({}, [])


def test_summary_marks_a_surprise():
    matrix = {'voyager': {'platform': 'lemmy', 'surface': 'web'}}
    table = summary(matrix, {'voyager': _result('voyager', '2.40.0', post='failed')})
    assert '| voyager | lemmy | web | 2.40.0 | passed | passed | **failed** (expected passed) |' in table


def test_summary_of_an_app_that_did_not_run():
    assert '| ghost |  |  | ? | **missing** (expected passed) |' in summary({'ghost': {}}, {})


def test_main_exits_1_and_writes_the_summary(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert (tmp_path / 'summary.md').exists()
    assert 'no result (the app did not run)' in capsys.readouterr().out


def test_main_exits_0_when_every_app_matches(tmp_path, capsys):
    for name, app in load_matrix().items():
        steps = {step: (app.get('expect', {}).get(step) or {'outcome': 'passed'})['outcome'] for step in STEPS}
        (tmp_path / f'{name}.json').write_text(json.dumps(_result(name, **steps)))
    assert main([str(tmp_path)]) == 0
    assert '0 problem(s)' in capsys.readouterr().out


def test_the_committed_matrix_is_well_formed():
    matrix = load_matrix(MATRIX)
    assert matrix
    assert compare(matrix, {name: _result(name, **{step: (app.get('expect', {}).get(step) or {'outcome': 'passed'})['outcome']
                                                   for step in STEPS}) for name, app in matrix.items()}) == []
    for name, app in matrix.items():
        assert app.get('platform') in ('mastodon', 'lemmy', 'pixelfed', 'peertube', 'castopod'), name
        assert app.get('surface') in ('web', 'android'), name
        assert sum(key in app for key in ('web', 'apk', 'probe', 'rss')) == 1, f'{name}: one way to check it'
        if 'apk' in app or 'web' in app:
            assert 'package' in app or 'web' in app, name
