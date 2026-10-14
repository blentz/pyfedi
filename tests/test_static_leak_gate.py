"""The static-tree leak gate: tests/static_leak_gate.py and its conftest hooks.

The gate fails a test that leaves a file under app/static/{media,tmp,posts}.
These pin what it counts as a leak, what it does not, and that the hooks in
tests/conftest.py fail, delete and report the way their comments say.
"""
import os
import types

import pytest

from tests import conftest
from tests import static_leak_gate
from tests.static_leak_gate import StaticWriteTracker


@pytest.fixture
def tracker(tmp_path):
    tracker = StaticWriteTracker([tmp_path / 'static'])
    (tmp_path / 'static').mkdir()
    tracker.start()
    return tracker


def opened(tracker, path, mode='w'):
    """What `open(path, mode)` raises, then the open itself."""
    tracker.audit('open', (path, mode, 0))
    open(path, mode).close()


def test_a_file_created_under_a_root_is_recorded(tracker, tmp_path):
    path = str(tmp_path / 'static' / 'new.png')
    opened(tracker, path)
    assert tracker.stop() == [path]


def test_a_file_created_outside_every_root_is_not(tracker, tmp_path):
    opened(tracker, str(tmp_path / 'elsewhere.png'))
    assert tracker.stop() == []


def test_a_root_is_matched_as_a_directory_not_a_prefix(tracker, tmp_path):
    """`static2/x` is not under `static`."""
    (tmp_path / 'static2').mkdir()
    opened(tracker, str(tmp_path / 'static2' / 'x.png'))
    assert tracker.stop() == []


def test_rewriting_a_file_that_already_existed_is_not_a_leak(tracker, tmp_path):
    path = tmp_path / 'static' / 'old.png'
    path.write_bytes(b'')
    opened(tracker, str(path))
    assert tracker.stop() == []


def test_a_file_removed_again_before_the_end_is_not_a_leak(tracker, tmp_path):
    path = str(tmp_path / 'static' / 'gone.png')
    opened(tracker, path)
    os.unlink(path)
    assert tracker.stop() == []


def test_reading_is_not_writing(tracker, tmp_path):
    tracker.audit('open', (str(tmp_path / 'static' / 'read.png'), 'r', 0))
    assert tracker.created == set()


def test_os_open_with_o_creat_counts(tracker, tmp_path):
    """`os.open` raises the event with mode None and the flags instead."""
    path = str(tmp_path / 'static' / 'raw')
    tracker.audit('open', (path, None, os.O_CREAT | os.O_WRONLY))
    assert tracker.created == {path}


def test_a_rename_into_a_root_counts_the_destination(tracker, tmp_path):
    source = tmp_path / 'staging.png'
    destination = str(tmp_path / 'static' / 'moved.png')
    tracker.audit('os.rename', (str(source), destination, -1, -1))
    assert tracker.created == {destination}


def test_a_file_descriptor_and_a_relative_path(tracker, tmp_path, monkeypatch):
    tracker.audit('open', (3, 'w', 0))
    assert tracker.created == set()
    monkeypatch.chdir(tmp_path)
    tracker.audit('open', ('static/relative.png', 'wb', 0))
    assert tracker.created == {str(tmp_path / 'static' / 'relative.png')}


def test_nothing_is_recorded_once_stopped(tracker, tmp_path):
    tracker.stop()
    tracker.audit('open', (str(tmp_path / 'static' / 'late.png'), 'w', 0))
    assert tracker.created == set()


def test_the_suites_tracker_is_installed_and_recording_during_a_test():
    """End to end, through the real audit hook: a file this test writes into
    app/static/tmp is recorded, and removed here so the gate passes."""
    path = os.path.join(conftest.REPO_ROOT, 'app/static/tmp', 'leak_gate_probe.txt')
    with open(path, 'w') as handle:
        handle.write('probe')
    try:
        assert path in conftest.STATIC_WRITES.created
    finally:
        os.unlink(path)


def drive_teardown(raises=None):
    """Run conftest's teardown wrapper around a teardown that returns or raises."""
    hook = conftest.pytest_runtest_teardown(types.SimpleNamespace(nodeid='t::leaky'), None)
    next(hook)
    if raises is None:
        hook.send(None)
    else:
        hook.throw(raises)


def test_the_teardown_hook_fails_the_test_names_the_file_and_deletes_it(
        tracker, tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, 'STATIC_WRITES', tracker)
    path = str(tmp_path / 'static' / 'left.png')
    opened(tracker, path)
    with pytest.raises(pytest.fail.Exception, match='left.png') as failure:
        drive_teardown()
    assert 't::leaky left 1 file(s)' in str(failure.value)
    assert not os.path.exists(path)


def test_the_teardown_hook_passes_a_test_that_left_nothing(tracker, monkeypatch):
    monkeypatch.setattr(conftest, 'STATIC_WRITES', tracker)
    with pytest.raises(StopIteration):
        drive_teardown()


def test_a_failing_teardown_keeps_its_own_error_and_still_deletes(
        tracker, tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, 'STATIC_WRITES', tracker)
    path = str(tmp_path / 'static' / 'left.png')
    opened(tracker, path)
    with pytest.raises(RuntimeError) as error:
        drive_teardown(raises=RuntimeError('fixture broke'))
    assert any('left.png' in note for note in error.value.__notes__)
    assert not os.path.exists(path)


def fake_session(tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, 'STATIC_TREES', (str(tmp_path / 'static'),))
    config = types.SimpleNamespace(stash=pytest.Stash())
    return types.SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)


def test_the_session_sweep_fails_the_run_and_deletes_what_appeared(tmp_path, monkeypatch):
    (tmp_path / 'static' / 'kept').mkdir(parents=True)
    (tmp_path / 'static' / 'kept' / 'old.png').write_bytes(b'')
    session = fake_session(tmp_path, monkeypatch)
    conftest.pytest_sessionstart(session)
    (tmp_path / 'static' / 'ab' / 'cd').mkdir(parents=True)
    (tmp_path / 'static' / 'ab' / 'cd' / 'new.png').write_bytes(b'')
    (tmp_path / 'static' / 'ef' / 'gh').mkdir(parents=True)

    conftest.pytest_sessionfinish(session, session.exitstatus)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert session.config.stash[conftest.STATIC_SWEEP_APPEARED] == [
        str(tmp_path / 'static' / 'ab' / 'cd' / 'new.png')]
    # What was there before is untouched; what the run made is gone, empty
    # directories included.
    assert sorted(os.listdir(tmp_path / 'static')) == ['kept']
    assert os.listdir(tmp_path / 'static' / 'kept') == ['old.png']


def test_a_clean_session_passes_and_keeps_a_directory_it_did_not_make(tmp_path, monkeypatch):
    (tmp_path / 'static' / 'empty').mkdir(parents=True)
    session = fake_session(tmp_path, monkeypatch)
    conftest.pytest_sessionstart(session)
    conftest.pytest_sessionfinish(session, session.exitstatus)
    assert session.exitstatus == pytest.ExitCode.OK
    assert session.config.stash[conftest.STATIC_SWEEP_APPEARED] == []
    assert os.listdir(tmp_path / 'static') == ['empty']


def test_a_worker_takes_no_snapshot(tmp_path, monkeypatch):
    """Only the controller sweeps: a worker would race the others."""
    session = fake_session(tmp_path, monkeypatch)
    session.config.workerinput = {}
    conftest.pytest_sessionstart(session)
    conftest.pytest_sessionfinish(session, session.exitstatus)
    assert conftest.STATIC_SWEEP_BEFORE not in session.config.stash


def test_remove_files_ignores_one_already_gone(tmp_path):
    static_leak_gate.remove_files([str(tmp_path / 'never-there')])
