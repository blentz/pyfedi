import os
import subprocess

import pytest

from app.shared import ffmpeg
from app.shared.ffmpeg import MediaToolError, extract_cover, extract_frame, run_ffprobe

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def test_probe_reads_a_real_mp4():
    probe = run_ffprobe(os.path.join(FIX, 'video_h264.mp4'))
    codecs = {s['codec_name'] for s in probe['streams']}
    assert 'h264' in codecs and float(probe['format']['duration']) > 0


def test_probe_of_non_media_raises():
    with pytest.raises(MediaToolError):
        run_ffprobe(os.path.join(FIX, 'not_media.mp4'))


def test_probe_command_is_sandboxed(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"streams": [], "format": {}}', stderr=b'')
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    run_ffprobe('/x/y.mp4')
    assert seen['cmd'][:9] == ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format',
                               '-show_streams', '-protocol_whitelist', 'file']
    assert seen['cmd'][-1] == 'file:/x/y.mp4'
    assert seen['timeout'] == 60 and seen['preexec_fn'] is ffmpeg._limit_address_space


def test_probe_timeout_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='timed out'):
        run_ffprobe('/x/y.mp4')


def test_probe_bad_json_raises(monkeypatch):
    monkeypatch.setattr(ffmpeg.subprocess, 'run',
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=b'not json', stderr=b''))
    with pytest.raises(MediaToolError, match='unreadable'):
        run_ffprobe('/x/y.mp4')


def test_tool_missing_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='not installed'):
        run_ffprobe('/x/y.mp4')


def test_rlimit_is_set(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg.resource, 'setrlimit', lambda which, lim: calls.append((which, lim)))
    ffmpeg._limit_address_space()
    assert calls == [(ffmpeg.resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))]


def test_frame_and_cover_are_written(tmp_path):
    frame, cover = tmp_path / 'f.jpg', tmp_path / 'c.jpg'
    extract_frame(os.path.join(FIX, 'video_h264.mp4'), 0.1, str(frame))
    extract_cover(os.path.join(FIX, 'audio_cover.mp3'), str(cover))
    assert frame.read_bytes()[:2] == b'\xff\xd8' and cover.read_bytes()[:2] == b'\xff\xd8'


def test_cover_of_audio_without_picture_raises(tmp_path):
    with pytest.raises(MediaToolError):
        extract_cover(os.path.join(FIX, 'audio.mp3'), str(tmp_path / 'c.jpg'))
