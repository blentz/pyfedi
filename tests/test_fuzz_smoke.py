"""tests/fuzz/smoke.py's own logic, without starting libFuzzer.

The smoke campaign is a gate: it is only worth running if a finding makes it
exit non-zero. These tests stand in for subprocess.run and check that it does --
for a crash artifact, a timeout artifact, a non-zero exit and a hung campaign --
and that a clean run, or a stale artifact from an earlier run, does not.
Nothing here imports atheris or spawns a campaign.
"""

import subprocess
import sys

import pytest

from tests.fuzz import smoke
from tests.fuzz.targets import REGISTRY


class TestParseArgs:

    def test_defaults_are_twenty_seconds_one_job_every_target(self):
        args = smoke.parse_args([])
        assert (args.seconds, args.jobs, args.targets) == (20, 1, sorted(REGISTRY))

    def test_named_targets_are_kept_in_order(self):
        names = sorted(REGISTRY)[::-1]
        assert smoke.parse_args(['--seconds', '5', *names]).targets == names

    @pytest.mark.parametrize('argv', [['no_such_target'], ['--seconds', '0'], ['--jobs', '0']])
    def test_bad_arguments_exit_with_usage(self, argv, capsys):
        with pytest.raises(SystemExit) as raised:
            smoke.parse_args(argv)
        assert raised.value.code == 2
        assert 'usage:' in capsys.readouterr().err


def test_the_campaign_command_routes_artifacts_out_of_the_committed_corpus():
    command = smoke.campaign_command('allowlist_html', 7)
    assert command == [sys.executable, '-m', 'tests.fuzz.run_campaign', 'allowlist_html',
                       '-max_total_time=7',
                       '-artifact_prefix=tests/fuzz/.work/smoke/allowlist_html/']


def test_the_smoke_artifact_prefix_overrides_the_campaign_default():
    """The prefix only keeps findings out of tests/fuzz/corpus/ if run_campaign
    does not also pass its own; campaign_argv drops a default the caller set."""
    from tests.fuzz import campaign
    command = smoke.campaign_command('allowlist_html', 7)
    argv = campaign.campaign_argv('prog', REGISTRY['allowlist_html'], command[4:])
    assert [a for a in argv if a.startswith('-artifact_prefix=')] == [
        '-artifact_prefix=tests/fuzz/.work/smoke/allowlist_html/']


class FakeRun:
    """Stands in for subprocess.run: writes `log` to the campaign's stdout, drops
    `artifacts` into the smoke artifact directory, returns `returncode` -- or
    raises TimeoutExpired when `hang` is set."""

    def __init__(self, returncode=0, artifacts=(), log='', hang=False):
        self.returncode, self.artifacts, self.log, self.hang = returncode, artifacts, log, hang
        self.calls = []

    def __call__(self, command, cwd, stdout, stderr, timeout):
        self.calls.append((command, cwd, timeout))
        stdout.write(self.log.encode())
        prefix = next(a for a in command if a.startswith('-artifact_prefix=')).partition('=')[2]
        for name in self.artifacts:
            (cwd / prefix / name).write_bytes(b'x')
        if self.hang:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, self.returncode)


DONE = '#1234\tDONE   cov: 90 ft: 513 corp: 40/2Kb exec/s: 2000 rss: 300Mb\nDone 1234 runs in 20 second(s)\n'


class TestRunTarget:

    def test_a_clean_run_passes_and_reports_libfuzzers_done_line(self, tmp_path):
        fake = FakeRun(log='#2\tINITED cov: 10\n' + DONE)
        result = smoke.run_target('allowlist_html', 20, root=tmp_path, run=fake)
        assert not result.failed
        assert result.summary.startswith('#1234\tDONE   cov: 90')
        command, cwd, timeout = fake.calls[0]
        assert cwd == tmp_path and timeout == 20 + smoke.GRACE_SECONDS

    @pytest.mark.parametrize('artifact', ['crash-abc', 'timeout-abc', 'oom-abc', 'leak-abc'])
    def test_a_finding_artifact_fails_the_target_even_on_exit_zero(self, tmp_path, artifact):
        result = smoke.run_target('allowlist_html', 20, root=tmp_path,
                                  run=FakeRun(artifacts=[artifact]))
        assert result.failed and result.artifacts == (artifact,)

    def test_a_slow_unit_alone_is_not_a_failure(self, tmp_path):
        result = smoke.run_target('allowlist_html', 20, root=tmp_path,
                                  run=FakeRun(artifacts=['slow-unit-abc']))
        assert not result.failed

    def test_a_non_zero_exit_fails_the_target(self, tmp_path):
        result = smoke.run_target('allowlist_html', 20, root=tmp_path, run=FakeRun(returncode=1))
        assert result.failed and result.returncode == 1

    def test_a_hung_campaign_fails_the_target(self, tmp_path):
        result = smoke.run_target('allowlist_html', 20, root=tmp_path, run=FakeRun(hang=True))
        assert result.failed and result.returncode is None

    def test_a_stale_artifact_from_an_earlier_run_is_cleared_first(self, tmp_path):
        stale = tmp_path / smoke.artifact_dir('allowlist_html')
        stale.mkdir(parents=True)
        (stale / 'crash-old').write_bytes(b'x')
        result = smoke.run_target('allowlist_html', 20, root=tmp_path, run=FakeRun())
        assert not result.failed

    def test_the_log_is_kept_beside_the_artifacts(self, tmp_path):
        smoke.run_target('allowlist_html', 20, root=tmp_path, run=FakeRun(log=DONE))
        log = tmp_path / smoke.artifact_dir('allowlist_html') / 'campaign.log'
        assert log.read_text() == DONE


class TestDescribe:

    def test_ok_line(self):
        assert smoke.describe(smoke.Result('t', 0, (), '#1 DONE cov: 1')) == 'ok    t: #1 DONE cov: 1'

    def test_ok_without_a_done_line(self):
        assert smoke.describe(smoke.Result('t', 0, (), '')) == 'ok    t: (no DONE line)'

    @pytest.mark.parametrize('result,why', [
        (smoke.Result('t', None, (), ''), 'did not finish within --seconds + 120s'),
        (smoke.Result('t', 0, ('crash-a', 'oom-b'), ''), 'left crash-a, oom-b'),
        (smoke.Result('t', 77, ('crash-a',), ''), 'left crash-a (exited 77)'),
        (smoke.Result('t', 3, (), ''), 'exited 3'),
    ])
    def test_fail_line_says_why_and_where_the_log_is(self, result, why):
        assert smoke.describe(result) == (
            f'FAIL  t: {why} -- see tests/fuzz/.work/smoke/t/campaign.log')


class TestMain:

    @staticmethod
    def _runner(failing=()):
        calls = []

        def runner(name, seconds):
            calls.append((name, seconds))
            return smoke.Result(name, 1 if name in failing else 0, (), '')
        return runner, calls

    def test_all_clean_exits_zero(self, capsys):
        runner, calls = self._runner()
        assert smoke.main(['--seconds', '3'], runner=runner) == 0
        assert calls == [(name, 3) for name in sorted(REGISTRY)]
        assert f'all {len(REGISTRY)} target(s) clean' in capsys.readouterr().out

    def test_any_failure_exits_non_zero_and_names_it(self, capsys):
        names = sorted(REGISTRY)
        runner, _ = self._runner(failing={names[0]})
        assert smoke.main(['--jobs', '2'], runner=runner) == 1
        assert f'1 of {len(names)} target(s) failed: {names[0]}' in capsys.readouterr().out
