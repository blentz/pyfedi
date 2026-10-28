import json
import re
from pathlib import Path

from interop.conformance.baseline import BASELINE, compare, main, parse_aptest_ndjson, parse_feditest_json, parse_feditest_locations, parse_tap

FIXTURES = Path(__file__).parent / 'fixtures' / 'conformance'
GAPS = Path(__file__).parent.parent / 'docs' / 'PYFEDI_GAPS.md'
TABLE_ROW = re.compile(r'^\| `([^`]+)` \| (\w+) \| (.*) \|$')
FAILED = {'outcome': 'failed', 'reason': 'r'}


def test_tap_outcomes():
    results = parse_tap((FIXTURES / 'sample.tap').read_text(), 'piefed')
    assert results == {
        'piefed/webfinger::resolves_local_account': 'passed',
        'piefed/activitypub::inbox_rejects_unsigned': 'failed',
        'piefed/nodeinfo::has_software_version': 'skipped',
    }


def test_tap_bail_out_is_an_error():
    assert parse_tap('TAP version 14\nBail out! no server\n', 'piefed') == {'piefed/__bail__': 'error'}


def test_aptest_outcomes():
    results = parse_aptest_ndjson((FIXTURES / 'sample.ndjson').read_text(), 'alice')
    assert results == {
        'aptest/alice/actor-objects-must-have-inbox-outbox-properties': 'passed',
        'aptest/alice/inbox-must-be-an-orderedcollection': 'failed',
        'aptest/alice/following-collection-must-be-a-collection': 'inapplicable',
        'aptest/alice/outbox-post-servers-must-return-a-201-created-http-code': 'cantTell',
        'aptest/alice/liked-collection-must-be-a-collection': 'error',
        'aptest/alice/by-url-only': 'passed',
    }


def test_ids_are_qualified_per_actor():
    text = (FIXTURES / 'sample.ndjson').read_text()
    both = parse_aptest_ndjson(text, 'alice') | parse_aptest_ndjson(text, 'microblogs')
    assert len(both) == 2 * len(parse_aptest_ndjson(text, 'alice'))


def test_new_failure_fails():
    assert any('x' in p for p in compare({'x': 'failed'}, {}))


def test_new_error_fails():
    assert any('x' in p for p in compare({'x': 'error'}, {}))


def test_baselined_failure_passes():
    assert compare({'x': 'failed'}, {'x': FAILED}) == []


def test_fixed_failure_must_leave_baseline():
    assert compare({'x': 'passed'}, {'x': FAILED}) != []


def test_missing_baselined_test_fails():
    assert compare({'y': 'passed'}, {'x': FAILED}) != []


def test_new_skip_fails_until_baselined():
    assert compare({'x': 'skipped'}, {}) != []
    assert compare({'x': 'skipped'}, {'x': {'outcome': 'skipped', 'reason': 'r'}}) == []


def test_an_inapplicable_test_that_becomes_cant_tell_fails():
    inapplicable = {'outcome': 'inapplicable', 'reason': 'r'}
    assert compare({'x': 'inapplicable'}, {'x': inapplicable}) == []
    assert compare({'x': 'cantTell'}, {'x': inapplicable}) == ['x: baselined as inapplicable but is now cantTell']
    assert compare({'x': 'cantTell'}, {}) == ['x: cantTell and not in the baseline']


def test_passing_test_needs_no_baseline():
    assert compare({'x': 'passed'}, {}) == []


def test_baselined_outcome_must_match():
    assert compare({'x': 'error'}, {'x': FAILED}) != []


def test_baseline_entry_without_reason_fails():
    assert compare({'x': 'failed'}, {'x': {'outcome': 'failed', 'reason': ''}}) != []
    assert compare({'x': 'failed'}, {'x': {'outcome': 'failed'}}) != []


def test_empty_results_fail_the_gate():
    assert compare({}, {}) == ['no results']


def test_feditest_json_outcomes():
    results = parse_feditest_json((FIXTURES / 'sample.feditest.json').read_text(), 'piefed')
    assert results == {
        'piefed/webfinger::resolves_local_account': 'passed',
        'piefed/activitypub::inbox_rejects_unsigned': 'failed',
        'piefed/nodeinfo::has_software_version': 'skipped',
        'piefed/activitypub::delivers_follow': 'error',
    }


def test_feditest_skip_is_not_a_pass():
    tap = parse_tap((FIXTURES / 'sample.tap').read_text(), 'piefed')
    results = parse_feditest_json((FIXTURES / 'sample.feditest.json').read_text(), 'piefed')
    assert results['piefed/nodeinfo::has_software_version'] != 'passed'
    assert compare({'piefed/nodeinfo::has_software_version': results['piefed/nodeinfo::has_software_version']}, {}) != []
    assert tap.keys() == {k for k in results if k in tap}


def test_tap_only_results_dir_is_no_results(tmp_path, capsys):
    (tmp_path / 'piefed.tap').write_text((FIXTURES / 'sample.tap').read_text())
    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == 'no results'


def test_cli_ignores_aptest_json(tmp_path, capsys):
    (tmp_path / 'aptest-alice.json').write_text((FIXTURES / 'sample.feditest.json').read_text())
    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == 'no results'


def test_cli_reads_results_dir(tmp_path, capsys):
    (tmp_path / 'piefed.json').write_text((FIXTURES / 'sample.feditest.json').read_text())
    (tmp_path / 'aptest-alice.ndjson').write_text((FIXTURES / 'sample.ndjson').read_text())
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert 'piefed/activitypub::inbox_rejects_unsigned' in out
    assert 'aptest/alice/inbox-must-be-an-orderedcollection' in out


def feditest_transcript(*run_tests, run_result=None, session_result=None):
    """A minimal transcript of one session over one test, in the shape of a real feditest --json file."""
    return json.dumps({
        'plan': {'session_template': {'tests': [{'name': 't::one', 'rolemapping': None, 'skip': None}]}},
        'test_meta': {'t::one': {'name': 't::one'}},
        'result': run_result,
        'sessions': [{'result': session_result, 'run_tests': list(run_tests)}],
    })


def run_test(result=None, run_steps=None):
    return {'plan_test_index': 0, 'result': result, 'run_steps': run_steps}


FAILURE = {'type': 'AssertionFailure'}
SOFT = {'type': 'SoftAssertionFailure'}
SKIP = {'type': 'SkipTestException'}


def test_real_feditest_json_outcomes():
    results = parse_feditest_json((FIXTURES / 'real.feditest.json').read_text(), 'webfinger')
    assert len(results) == 18
    assert results['webfinger/webfinger.server.4_2__3_requires_resource_uri::requires_resource_uri_http_status'] == 'failed'
    assert results['webfinger/webfinger.server.4_5__1_any_uri_scheme_for_resource_identifiers::any_uri_scheme_for_resource_identifiers'] == 'failed'
    assert sorted(set(results.values())) == ['failed', 'passed']
    assert list(results.values()).count('failed') == 2


def test_a_set_test_result_wins_over_a_passing_step():
    text = feditest_transcript(run_test(FAILURE, [{'result': None}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'failed'}


def test_a_set_test_result_wins_over_a_step_error():
    text = feditest_transcript(run_test(SKIP, [{'result': {'type': 'ValueError'}}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'skipped'}


def test_a_failing_step_fails_a_test_with_no_result():
    text = feditest_transcript(run_test(None, [{'result': None}, {'result': FAILURE}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'failed'}


def test_a_soft_step_failure_still_counts_when_it_is_all_there_is():
    text = feditest_transcript(run_test(None, [{'result': SOFT}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'failed'}


def test_a_later_hard_step_failure_replaces_a_soft_one():
    text = feditest_transcript(run_test(None, [{'result': SOFT}, {'result': SKIP}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'skipped'}


def test_steps_that_all_pass_leave_the_test_passed():
    text = feditest_transcript(run_test(None, [{'result': None}, {'result': None}]))
    assert parse_feditest_json(text, 'p') == {'p/t::one': 'passed'}


def test_a_run_level_result_is_an_error_entry():
    text = feditest_transcript(run_test(), run_result=FAILURE)
    assert parse_feditest_json(text, 'p')['p/__run__'] == 'error'


def test_a_session_level_result_is_an_error_entry():
    text = feditest_transcript(run_test(), session_result=FAILURE)
    assert parse_feditest_json(text, 'p')['p/__session_0__'] == 'error'


TESTS_DIR = '/opt/feditest-tests-fediverse/tests/system2'


def failure_at(*frames):
    return {'type': 'AssertionFailure', 'stacktrace': [[f'{TESTS_DIR}/{name}', line] for name, line in frames]}


def test_a_failure_is_located_at_its_innermost_frame_in_the_tests_dir():
    result = failure_at(('reply_single_hop.py', 10), ('reply_single_hop.py', 76))
    result['stacktrace'].append(['/usr/lib/python3/hamcrest/core/assert_that.py', 57])
    text = feditest_transcript(run_test(None, [{'result': None}, {'result': result}]))
    assert parse_feditest_locations(text, 'p') == {'p/t::one': 'reply_single_hop.py:76'}


def test_a_test_result_is_located_like_a_step_result():
    text = feditest_transcript(run_test(failure_at(('follow_receive.py', 81)), None))
    assert parse_feditest_locations(text, 'p') == {'p/t::one': 'follow_receive.py:81'}


def test_a_passing_test_and_a_skip_have_no_location():
    assert parse_feditest_locations(feditest_transcript(run_test()), 'p') == {}
    skip = {'type': 'SkipTestException', 'stacktrace': [[f'{TESTS_DIR}/x.py', 3]]}
    assert parse_feditest_locations(feditest_transcript(run_test(skip)), 'p') == {}


def test_a_failure_outside_the_tests_dir_has_an_unknown_location():
    result = {'type': 'ValueError', 'stacktrace': [['/app/driver.py', 5]]}
    assert parse_feditest_locations(feditest_transcript(run_test(result)), 'p') == {'p/t::one': 'unknown'}


def test_the_sample_transcript_has_no_frame_in_the_tests_dir():
    locations = parse_feditest_locations((FIXTURES / 'sample.feditest.json').read_text(), 'piefed')
    assert set(locations.values()) == {'unknown'}
    assert len(locations) == 2


AT = {'outcome': 'failed', 'reason': 'r', 'at': 'reply_single_hop.py:76'}


def test_a_baselined_failure_at_its_baselined_line_passes():
    assert compare({'x': 'failed'}, {'x': AT}, {'x': 'reply_single_hop.py:76'}) == []


def test_a_baselined_failure_elsewhere_fails():
    problems = compare({'x': 'failed'}, {'x': AT}, {'x': 'reply_single_hop.py:79'})
    assert problems == ['x: baselined as failed at reply_single_hop.py:76 but fails elsewhere, at reply_single_hop.py:79']


def test_a_baselined_failure_with_an_unknown_location_fails():
    assert compare({'x': 'failed'}, {'x': AT}, {'x': 'unknown'}) != []


def test_a_located_failure_needs_an_at_in_its_entry():
    assert compare({'x': 'failed'}, {'x': FAILED}, {'x': 'follow_receive.py:81'}) == ['x: baseline entry has no at']


def test_an_outcome_mismatch_is_not_also_reported_as_elsewhere():
    assert len(compare({'x': 'error'}, {'x': AT}, {'x': 'follow_receive.py:81'})) == 1


def test_cli_names_a_baselined_test_that_fails_elsewhere(tmp_path, capsys, monkeypatch):
    (tmp_path / 'results').mkdir()
    baseline = {'p/t::one': {'outcome': 'failed', 'reason': 'r', 'at': 'reply_single_hop.py:76'}}
    (tmp_path / 'baseline.json').write_text(json.dumps(baseline))
    monkeypatch.setattr('interop.conformance.baseline.BASELINE', tmp_path / 'baseline.json')
    (tmp_path / 'results' / 'p.json').write_text(feditest_transcript(run_test(failure_at(('reply_single_hop.py', 79)))))
    assert main([str(tmp_path / 'results')]) == 1
    assert 'fails elsewhere, at reply_single_hop.py:79' in capsys.readouterr().out
    (tmp_path / 'results' / 'p.json').write_text(feditest_transcript(run_test(failure_at(('reply_single_hop.py', 76)))))
    assert main([str(tmp_path / 'results')]) == 0


def test_the_gaps_table_has_the_baseline_rows_word_for_word():
    """The README says to add the same row to the "External conformance suites" table of docs/PYFEDI_GAPS.md."""
    section = GAPS.read_text().split('### External conformance suites', 1)[1].split('\n## ', 1)[0]
    table = {m.group(1): (m.group(2), m.group(3)) for m in map(TABLE_ROW.match, section.splitlines()) if m}
    baseline = {test_id: (entry['outcome'], entry['reason']) for test_id, entry in json.loads(BASELINE.read_text()).items()}
    assert table == baseline
