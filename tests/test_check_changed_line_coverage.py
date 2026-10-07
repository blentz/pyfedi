"""tests/check_changed_line_coverage.py: the gate that fails a branch for every line it added and no test ran.

The diff reader is replaced with canned `git diff --unified=0` text (the test
container has no git history to read), so each test states the diff it is about
and the coverage.json fragment it is judged against. `run_git` itself is
exercised against a stub of subprocess.run, which is the only part that touches
the outside world.
"""
import json
import subprocess

import pytest

from tests import check_changed_line_coverage as gate


def diff_for(path, *hunks):
    """A `git diff --unified=0` for one file. A hunk is (new_start, new_count)."""
    text = f'diff --git a/{path} b/{path}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}\n'
    for start, count in hunks:
        header = f'@@ -{start},0 +{start},{count} @@' if count != 1 else f'@@ -{start} +{start} @@'
        text += header + '\n' + ''.join('+x\n' for _ in range(count))
    return text


def report(**files):
    """coverage.json fragment: name=(executed, missing) keyed by the path with '__' for '/' and '_py' for '.py'."""
    data = {'files': {}}
    for name, (executed, missing) in files.items():
        key = name.replace('__', '/').replace('_py', '.py')
        data['files'][key] = {'executed_lines': executed, 'missing_lines': missing}
    return data


class TestParseAddedLines:

    def test_a_multi_line_hunk_adds_every_line_in_its_range(self):
        assert gate.parse_added_lines(diff_for('app/a.py', (10, 3))) == {'app/a.py': {10, 11, 12}}

    def test_a_hunk_with_no_count_adds_exactly_one_line(self):
        assert gate.parse_added_lines(diff_for('app/a.py', (7, 1))) == {'app/a.py': {7}}

    def test_a_pure_deletion_adds_nothing(self):
        text = 'diff --git a/app/a.py b/app/a.py\n--- a/app/a.py\n+++ b/app/a.py\n@@ -5,2 +4,0 @@\n-x\n-y\n'

        assert gate.parse_added_lines(text) == {}

    def test_several_hunks_and_several_files_are_kept_apart(self):
        text = diff_for('app/a.py', (1, 2), (20, 1)) + diff_for('app/b.py', (5, 1))

        assert gate.parse_added_lines(text) == {'app/a.py': {1, 2, 20}, 'app/b.py': {5}}

    def test_a_deleted_file_has_no_added_lines(self):
        text = ('diff --git a/app/gone.py b/app/gone.py\n--- a/app/gone.py\n+++ /dev/null\n'
                '@@ -1,2 +0,0 @@\n-x\n-y\n')

        assert gate.parse_added_lines(text) == {}

    def test_a_hunk_header_before_any_file_header_is_ignored(self):
        assert gate.parse_added_lines('@@ -1,0 +1,2 @@\n') == {}

    def test_lines_that_are_neither_headers_nor_hunks_are_ignored(self):
        text = 'index 1..2\nBinary files differ\n' + diff_for('app/a.py', (3, 1))

        assert gate.parse_added_lines(text) == {'app/a.py': {3}}

    def test_a_malformed_hunk_header_is_an_error_not_silence(self):
        text = '+++ b/app/a.py\n@@ nonsense @@\n'

        with pytest.raises(gate.GateError, match='hunk header'):
            gate.parse_added_lines(text)


class TestReportKeys:

    def test_both_key_styles_resolve_to_the_repository_path(self):
        files = {'app/a.py': {'x': 1}, '/app/app/b.py': {'x': 2}}

        assert gate.repo_relative_files(files) == {'app/a.py': {'x': 1}, 'app/b.py': {'x': 2}}

    def test_a_key_that_is_neither_is_kept_as_it_is(self):
        assert gate.repo_relative_files({'other/c.py': {}}) == {'other/c.py': {}}


class TestUncoveredAddedLines:

    def test_an_added_line_listed_as_missing_is_reported(self):
        data = report(app__a_py=([1, 2], [3, 4]))

        found, unmeasured = gate.uncovered_added_lines({'app/a.py': {2, 3, 4}}, data)

        assert found == {'app/a.py': [3, 4]}
        assert unmeasured == []

    def test_an_added_line_that_is_not_a_statement_is_ignored(self):
        """A comment, a blank line or a docstring is in neither executed_lines nor missing_lines."""
        data = report(app__a_py=([1], [9]))

        found, _ = gate.uncovered_added_lines({'app/a.py': {5, 6, 7}}, data)

        assert found == {}

    def test_a_missing_line_that_was_not_added_is_not_this_diffs_problem(self):
        data = report(app__a_py=([1], [9]))

        found, _ = gate.uncovered_added_lines({'app/a.py': {1}}, data)

        assert found == {}

    def test_a_container_style_key_is_matched_to_the_repository_path(self):
        data = {'files': {'/app/app/a.py': {'executed_lines': [], 'missing_lines': [3]}}}

        found, _ = gate.uncovered_added_lines({'app/a.py': {3}}, data)

        assert found == {'app/a.py': [3]}

    def test_a_python_file_the_report_does_not_know_is_unmeasured(self):
        found, unmeasured = gate.uncovered_added_lines({'app/new.py': {1}}, {'files': {'app/a.py': {}}})

        assert found == {}
        assert unmeasured == ['app/new.py']

    def test_a_file_that_is_not_python_is_never_judged(self):
        found, unmeasured = gate.uncovered_added_lines({'app/templates/x.html': {1}}, {'files': {'app/a.py': {}}})

        assert (found, unmeasured) == ({}, [])

    def test_a_report_entry_without_line_lists_counts_nothing_as_missing(self):
        data = {'files': {'app/a.py': {}}}

        assert gate.uncovered_added_lines({'app/a.py': {1}}, data)[0] == {}


class TestRunGit:

    def test_the_diff_is_asked_for_over_base_to_head_on_the_given_paths(self, monkeypatch):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout='the diff', stderr='')

        monkeypatch.setattr(gate.subprocess, 'run', fake_run)

        assert gate.run_git('abc123', ['app/a.py', 'app/b/']) == 'the diff'
        assert calls == [['git', 'diff', '--unified=0', '--no-color', '--no-ext-diff', 'abc123..HEAD',
                          '--', 'app/a.py', 'app/b/']]

    def test_a_failing_git_is_an_error_carrying_its_message(self, monkeypatch):
        monkeypatch.setattr(gate.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(
            command, 128, stdout='', stderr='fatal: bad revision'))

        with pytest.raises(gate.GateError, match='bad revision'):
            gate.run_git('nope', ['app/'])

    def test_a_missing_git_binary_is_an_error(self, monkeypatch):
        def no_git(command, **kwargs):
            raise FileNotFoundError('git')

        monkeypatch.setattr(gate.subprocess, 'run', no_git)

        with pytest.raises(gate.GateError, match='cannot run git'):
            gate.run_git('abc', ['app/'])


@pytest.fixture
def cov_path(tmp_path):
    def write(data):
        path = tmp_path / 'coverage.json'
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return str(path)
    return write


@pytest.fixture
def canned_diff(monkeypatch):
    seen = []

    def install(text):
        def fake(base_ref, paths):
            seen.append((base_ref, list(paths)))
            return text
        monkeypatch.setattr(gate, 'run_git', fake)
        return seen
    return install


class TestMain:

    def test_every_added_line_covered_passes(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 2)))
        path = cov_path(report(app__a_py=([3, 4], [])))

        assert gate.main([path, 'base']) == 0
        assert 'Every added line' in capsys.readouterr().out

    def test_an_added_uncovered_line_fails_and_is_named(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 2)))
        path = cov_path(report(app__a_py=([3], [4])))

        assert gate.main([path, 'base']) == 1
        assert 'app/a.py:4' in capsys.readouterr().err

    def test_a_diff_with_no_added_lines_passes(self, cov_path, canned_diff):
        canned_diff('')
        path = cov_path(report(app__a_py=([1], [2])))

        assert gate.main([path, 'base']) == 0

    def test_with_no_paths_the_whole_of_app_is_diffed(self, cov_path, canned_diff):
        seen = canned_diff('')

        gate.main([cov_path(report(app__a_py=([1], []))), 'base'])

        assert seen == [('base', ['app/'])]

    def test_given_paths_are_diffed_instead(self, cov_path, canned_diff):
        seen = canned_diff('')

        gate.main([cov_path(report(app__a_py=([1], []))), 'base', 'app/utils.py', 'app/community'])

        assert seen == [('base', ['app/utils.py', 'app/community'])]

    def test_an_unmeasured_python_file_fails_closed(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/new.py', (1, 1)))
        path = cov_path(report(app__a_py=([1], [])))

        assert gate.main([path, 'base']) == 2
        assert 'app/new.py' in capsys.readouterr().err

    def test_uncovered_lines_are_all_listed_across_files(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (1, 1)) + diff_for('app/b.py', (9, 1)))
        path = cov_path(report(app__a_py=([], [1]), app__b_py=([], [9])))

        assert gate.main([path, 'base']) == 1
        err = capsys.readouterr().err
        assert 'app/a.py:1' in err and 'app/b.py:9' in err

    def test_the_reports_path_and_age_are_printed(self, cov_path, canned_diff, capsys):
        canned_diff('')
        path = cov_path(report(app__a_py=([1], [])))

        gate.main([path, 'base'])

        assert f'Coverage report: {path}' in capsys.readouterr().out

    def test_too_few_arguments_is_a_usage_error(self, capsys):
        assert gate.main(['coverage.json']) == 2
        assert 'Usage' in capsys.readouterr().err

    def test_a_missing_report_is_an_error_not_a_pass(self, tmp_path, canned_diff, capsys):
        canned_diff('')

        assert gate.main([str(tmp_path / 'absent.json'), 'base']) == 2
        assert 'cannot read coverage report' in capsys.readouterr().err

    def test_a_report_that_is_not_json_is_an_error(self, cov_path, canned_diff, capsys):
        canned_diff('')

        assert gate.main([cov_path('not json'), 'base']) == 2
        assert 'not valid JSON' in capsys.readouterr().err

    @pytest.mark.parametrize('body', [{}, {'files': {}}, {'files': []}, []])
    def test_a_report_with_no_files_is_an_error(self, cov_path, canned_diff, capsys, body):
        """An empty report would let every added line through as 'not a statement'."""
        canned_diff(diff_for('app/a.py', (1, 1)))

        assert gate.main([cov_path(body), 'base']) == 2
        assert 'lists no files' in capsys.readouterr().err

    def test_a_failing_git_is_an_error_not_a_pass(self, cov_path, monkeypatch, capsys):
        def broken(base_ref, paths):
            raise gate.GateError('git diff failed: fatal: bad revision')
        monkeypatch.setattr(gate, 'run_git', broken)

        assert gate.main([cov_path(report(app__a_py=([1], []))), 'nope']) == 2
        assert 'bad revision' in capsys.readouterr().err

    def test_a_path_that_does_not_exist_is_an_error(self, cov_path, canned_diff, capsys):
        """A typo'd path makes git diff print nothing, which would read as 'no added lines'."""
        canned_diff('')

        assert gate.main([cov_path(report(app__a_py=([1], []))), 'base', 'app/no_such_module.py']) == 2
        assert 'no_such_module' in capsys.readouterr().err


def branch_report(path, executed, missing, missing_branches):
    """coverage.json fragment for one file, with branch data."""
    return {'files': {path: {'executed_lines': executed, 'missing_lines': missing,
                             'missing_branches': missing_branches}}}


class TestUncoveredAddedBranches:

    def test_an_untaken_branch_out_of_an_added_line_is_reported(self):
        added = {'app/a.py': {3, 4}}
        data = branch_report('app/a.py', [3, 4, 5], [], [[3, 5], [9, -1]])

        assert gate.uncovered_added_branches(added, data) == {'app/a.py': [(3, 5)]}

    def test_a_branch_out_of_a_line_that_was_not_added_is_not_this_diffs_problem(self):
        added = {'app/a.py': {3}}
        data = branch_report('app/a.py', [3, 9], [], [[9, -1]])

        assert gate.uncovered_added_branches(added, data) == {}

    def test_files_the_report_lacks_and_non_python_files_are_skipped(self):
        added = {'app/b.py': {1}, 'app/t.html': {1}}
        data = branch_report('app/a.py', [1], [], [[1, 2]])

        assert gate.uncovered_added_branches(added, data) == {}

    def test_an_entry_without_branch_data_counts_nothing_as_missing(self):
        added = {'app/a.py': {1}}

        assert gate.uncovered_added_branches(added, report(app__a_py=([1], []))) == {}


class TestMainBranches:

    def test_an_untaken_branch_fails_and_is_named(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, -1]]))

        assert gate.main([path, 'base', '--branches']) == 1
        assert 'app/a.py:3: branch to exit was never taken' in capsys.readouterr().err

    def test_a_branch_to_a_line_is_named_by_that_line(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, 7]]))

        assert gate.main([path, 'base', '--branches']) == 1
        assert 'app/a.py:3: branch to 7 was never taken' in capsys.readouterr().err

    def test_every_branch_taken_passes(self, cov_path, canned_diff):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], []))

        assert gate.main([path, 'base', '--branches']) == 0

    def test_without_the_flag_branches_are_not_judged(self, cov_path, canned_diff):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, -1]]))

        assert gate.main([path, 'base']) == 0

    def test_the_flag_is_not_read_as_a_path(self, cov_path, canned_diff):
        seen = canned_diff('')

        gate.main([cov_path(branch_report('app/a.py', [1], [], [])), 'base', '--branches', 'app/community'])

        assert seen == [('base', ['app/community'])]

    def test_a_report_with_no_branch_data_fails_closed_under_the_flag(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(report(app__a_py=([3], [])))

        assert gate.main([path, 'base', '--branches']) == 2
        assert '--cov-branch' in capsys.readouterr().err

    def test_lines_and_branches_both_reported_in_one_run(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 2)))
        path = cov_path(branch_report('app/a.py', [3], [4], [[3, 4]]))

        assert gate.main([path, 'base', '--branches']) == 1
        err = capsys.readouterr().err
        assert 'app/a.py:4: added line was not executed' in err
        assert 'app/a.py:3: branch to 4 was never taken' in err
