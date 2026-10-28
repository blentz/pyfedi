from pathlib import Path

from interop.conformance.baseline import compare, main, parse_aptest_ndjson, parse_tap

FIXTURES = Path(__file__).parent / 'fixtures' / 'conformance'
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
        'aptest/alice/following-collection-must-be-a-collection': 'skipped',
        'aptest/alice/outbox-post-servers-must-return-a-201-created-http-code': 'skipped',
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


def test_passing_test_needs_no_baseline():
    assert compare({'x': 'passed'}, {}) == []


def test_baselined_outcome_must_match():
    assert compare({'x': 'error'}, {'x': FAILED}) != []


def test_baseline_entry_without_reason_fails():
    assert compare({'x': 'failed'}, {'x': {'outcome': 'failed', 'reason': ''}}) != []
    assert compare({'x': 'failed'}, {'x': {'outcome': 'failed'}}) != []


def test_empty_results_fail_the_gate():
    assert compare({}, {}) == ['no results']


def test_cli_reads_results_dir(tmp_path, capsys):
    (tmp_path / 'piefed.tap').write_text((FIXTURES / 'sample.tap').read_text())
    (tmp_path / 'aptest-alice.ndjson').write_text((FIXTURES / 'sample.ndjson').read_text())
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert 'piefed/activitypub::inbox_rejects_unsigned' in out
    assert 'aptest/alice/inbox-must-be-an-orderedcollection' in out
